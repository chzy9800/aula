"""讲义大纲：把整份 PDF 压成一棵可点击的目录树。

设计照搬自对 BBNotes 的逆向分析（docs 里那套取证），因为它解决了两个
"让模型做目录"的经典坑：

1. **标题翻译与原文定位解耦。** ``display_title`` 翻译成界面语言，
   ``search_anchor`` 强制保留原文逐字（提示词里明写"禁止翻译"）。
   界面显示中文，点击却能在原始 PDF 里精确搜到那一段。
2. **页码不让模型数。** 输入里预埋 ``[[PAGE_n]]`` 标记，模型只被要求
   "取最近的标记"，比让它自己推断页码可靠得多。

长文档按字符预算切块，逐块生成再合并：同一段原文只留一个主题
（按 search_anchor 去重），按页码排序。
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Dict, List, Optional

from .llm import LLMClient, LLMError

CHUNK_BUDGET = 6000
MAX_TOPICS = 80

OUTLINE_SYSTEM = """你是讲义分析员。用户给你一份 PDF 讲义的文本，页面之间用 [[PAGE_n]] 标记分隔（n 是页码）。

你的任务是输出一棵层级大纲，严格 JSON：

{{
  "topics": [
    {{
      "display_title": "主题标题，翻译成{lang}，不超过 24 字",
      "search_anchor": "原文逐字文本，10–80 个字符，必须是讲义里真实出现的连续文字",
      "page_index": 出现的页码（整数），
      "difficulty_score": 1–100 的难度分，
      "subtopics": [同样结构的子主题，可以为空数组]
    }}
  ]
}}

硬性要求：
1. **search_anchor 绝对禁止翻译**，必须与原文逐字一致（包括大小写与标点），
   因为它要拿去在原始 PDF 里做文本搜索。display_title 才是翻译后的标题。
2. page_index 取 search_anchor 之前最近的 [[PAGE_n]] 标记的 n。
3. 只提取真正的章节与主题，不要为每个段落建条目；顶层 topics 控制在 3–15 个。
4. difficulty_score 衡量理解难度：定义性内容 20–40，推导与计算 50–75，
   多概念纠缠的 80 以上。
5. 文本可能有提取噪声（断行、连字符），search_anchor 选一段干净连续的原文。
6. 没有可提取的主题就返回 {{"topics": []}}。
7. 只输出 JSON，不要解释文字。"""

PAGE_MARK = re.compile(r"\[\[PAGE_(\d+)\]\]")


def build_pages_text(pages: List[Dict[str, Any]]) -> str:
    """把 [{index, text}] 拼成带页标记的单文本，空页跳过。"""
    parts: List[str] = []
    for page in pages:
        text = str(page.get("text") or "").strip()
        if not text:
            continue
        parts.append("[[PAGE_%d]]\n%s" % (int(page.get("index") or 0), text))
    return "\n".join(parts)


def split_chunks(text: str, budget: int = CHUNK_BUDGET) -> List[str]:
    """按段落边界切块，块不超过 budget；每块都自带页标记。

    两个细节是测试逼出来的：
    1. ``[[PAGE_n]]`` 标记必须和它后面那段正文绑成同一个单元——否则块的
       边界正好落在两者之间时，那一页的正文就没有页标记，模型只能瞎猜。
    2. 新块开头如果不是页标记，补上最近见过的那个。让模型始终知道
       "我现在读到第几页"，而不是靠上下文外推。
    """
    if len(text) <= budget:
        return [text] if text.strip() else []

    lines = text.split("\n")
    units: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if PAGE_MARK.fullmatch(line.strip()) and index + 1 < len(lines):
            units.append(line + "\n" + lines[index + 1])
            index += 2
        else:
            units.append(line)
            index += 1

    chunks: List[str] = []
    current: List[str] = []
    size = 0
    last_mark = ""
    for unit in units:
        match = PAGE_MARK.match(unit.strip())
        if match:
            last_mark = "[[PAGE_%s]]" % match.group(1)
        cost = len(unit) + 1
        if current and size + cost > budget:
            if not current[0].startswith("[[PAGE_") and last_mark:
                current.insert(0, last_mark)
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(unit)
        size += cost
    if current:
        if not current[0].startswith("[[PAGE_") and last_mark:
            current.insert(0, last_mark)
        chunks.append("\n".join(current))
    return [chunk for chunk in chunks if chunk.strip()]


def normalize_anchor(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip().lower()


def merge_topics(chunk_topics: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """合并各块结果：按 search_anchor 去重（保留最先出现的），按页码排序。"""
    seen: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for topics in chunk_topics:
        for topic in topics:
            if not isinstance(topic, dict):
                continue
            anchor = normalize_anchor(topic.get("search_anchor"))
            if not anchor:
                continue
            if anchor in seen:
                # 重复出现时把子主题并进来，不重复建顶层条目
                existing = seen[anchor]
                for child in topic.get("subtopics") or []:
                    if isinstance(child, dict):
                        child_anchor = normalize_anchor(child.get("search_anchor"))
                        if child_anchor and not any(
                            normalize_anchor(item.get("search_anchor")) == child_anchor
                            for item in existing.get("subtopics") or []
                        ):
                            existing.setdefault("subtopics", []).append(child)
                continue
            cleaned = _clean_topic(topic)
            if cleaned is not None:
                seen[anchor] = cleaned
                order.append(anchor)

    topics = [seen[anchor] for anchor in order]
    topics.sort(key=lambda item: (item.get("page_index") or 0))
    return topics[:MAX_TOPICS]


def _clean_topic(topic: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    title = str(topic.get("display_title") or "").strip()
    anchor = str(topic.get("search_anchor") or "").strip()
    if not title or not anchor:
        return None

    def children(value: Any) -> List[Dict[str, Any]]:
        result: List[Dict[str, Any]] = []
        for child in value or []:
            if isinstance(child, dict):
                cleaned = _clean_topic(child)
                if cleaned is not None:
                    result.append(cleaned)
        return result

    try:
        page_index = int(topic.get("page_index") or 0)
    except (TypeError, ValueError):
        page_index = 0
    try:
        difficulty = max(1, min(100, int(topic.get("difficulty_score") or 40)))
    except (TypeError, ValueError):
        difficulty = 40

    return {
        "display_title": title[:60],
        "search_anchor": anchor[:160],
        "page_index": page_index,
        "difficulty_score": difficulty,
        "subtopics": children(topic.get("subtopics")),
    }


def build_outline(
    pages: List[Dict[str, Any]],
    client: LLMClient,
    target_lang: str = "zh",
    progress: Optional[Any] = None,
) -> Dict[str, Any]:
    """完整流程：拼页 → 分块 → 逐块生成 → 合并去重。"""
    text = build_pages_text(pages)
    if not text.strip():
        raise LLMError("讲义没有可提取的文本（可能是扫描件），无法生成大纲")

    chunks = split_chunks(text)
    if progress:
        progress("共 %d 页，分 %d 块处理" % (len(pages), len(chunks)))

    collected: List[List[Dict[str, Any]]] = []
    failures = 0
    for index, chunk in enumerate(chunks, 1):
        message = [
            {"role": "system", "content": OUTLINE_SYSTEM.format(lang=target_lang)},
            {"role": "user", "content": chunk},
        ]
        try:
            raw = client.chat_json(message, max_tokens=3000)
        except LLMError as exc:
            # 单块解析失败只丢这一块，不让整份大纲报废；
            # 但全部块都失败时必须报错——返回"空大纲"会让用户以为讲义没内容。
            failures += 1
            if progress:
                progress("第 %d/%d 块失败：%s" % (index, len(chunks), str(exc)[:60]))
            collected.append([])
            continue
        topics = raw.get("topics") if isinstance(raw, dict) else None
        collected.append(topics if isinstance(topics, list) else [])
        if progress:
            progress("第 %d/%d 块完成（%d 个主题）" % (index, len(chunks), len(collected[-1])))

    if failures == len(chunks):
        raise LLMError("全部 %d 块都解析失败，检查模型是否可用（先跑 `aula doctor`）" % len(chunks))

    topics = merge_topics(collected)
    return {"topics": topics, "chunks": len(chunks)}


def cache_key(pages: List[Dict[str, Any]], target_lang: str) -> str:
    payload = json_dumps(pages) + "|" + target_lang
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def json_dumps(value: Any) -> str:
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True)
