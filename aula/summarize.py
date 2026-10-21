"""总结：把一节课的转写与手写笔记合成一份结构化记录。

和离线版（apuntes）同一套思路，但有一处不同：课堂笔记必须尊重**用户手写
的内容**。模型容易把手写笔记当成"另一份转写"重新概括，导致用户自己记的
重点被稀释。所以提示词里明确要求：手写笔记里出现的内容视为最高优先级。
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from .llm import LLMClient, LLMError

SUMMARY_SYSTEM = """你是一名课堂记录整理助手。用户会给你一节课的转写（外语原文 + 中文译文）以及学生自己记的笔记。

请输出一份完整的课堂总结，严格输出 JSON：

{
  "overview": "2-3 句话概括这节课讲了什么",
  "points": [{"heading": "要点小标题（≤12字）", "detail": "2-3 句展开，讲清逻辑"}],
  "terms": [{"term": "外文原词", "translation": "中文译名", "note": "一句话解释"}],
  "todos": ["需要完成的事，含截止时间或提交要求"],
  "questions": ["课后应该自己查清的问题，具体可执行"],
  "emphasis": ["老师明确强调、重复、或提示会考的内容"]
}

硬性要求：
1. **学生手写笔记里出现的内容优先级最高**，必须体现在 points 里，不能被概括掉。
2. 只写转写和笔记里真实存在的内容，绝不编造。没有的字段给空数组。
3. points 4-8 条，保持课堂原有的推进顺序。
4. todos 只写真实的作业、阅读、提交、下节课要求；没有就是空数组。
5. questions 至少 2 条，要具体到"查什么"，不要写"复习本节内容"。
6. 只输出 JSON。"""

CHUNK_SYSTEM = """你在为课堂总结做预处理。把这段转写压缩成要点，只抽取不发挥。

输出 JSON：
{
  "points": ["这段讲了什么，一条一句"],
  "terms": [{"term": "外文原词", "translation": "中文", "note": "一句话"}],
  "todos": ["提到的作业或要求"],
  "emphasis": ["老师强调或提示会考的内容"]
}

要求：只写原文真实出现的内容；points 3-6 条；只输出 JSON。"""


def _mmss(ms: Any) -> str:
    try:
        total = int(ms) // 1000
    except (TypeError, ValueError):
        return "00:00"
    return "%02d:%02d" % (total // 60, total % 60)


def render_utterances(utterances: List[Dict[str, Any]]) -> str:
    lines = []
    for item in utterances:
        source = (item.get("source") or "").strip()
        target = (item.get("target") or "").strip()
        if not source and not target:
            continue
        stamp = _mmss(item.get("start_ms"))
        if source:
            lines.append("[%s] %s" % (stamp, source))
        if target:
            lines.append("译：%s" % target)
    return "\n".join(lines)


def _chunks(text: str, budget: int = 4000) -> List[str]:
    blocks = [block for block in text.split("\n\n") if block.strip()]
    if not blocks:
        return []
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for block in blocks:
        if current and size + len(block) > budget:
            chunks.append("\n\n".join(current))
            current = [block]
            size = len(block)
        else:
            current.append(block)
            size += len(block)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def summarize(
    client: LLMClient,
    model: str,
    utterances: List[Dict[str, Any]],
    note: str = "",
    budget: int = 8000,
    timeout: int = 180,
) -> Dict[str, Any]:
    """生成总结。文本过长时先逐块压缩，再合成。"""
    transcript = render_utterances(utterances)
    if not transcript.strip() and not note.strip():
        raise LLMError("这一节课还没有任何转写或笔记，无法总结")

    payload: str
    if len(transcript) <= budget:
        payload = "【课堂转写】\n%s\n\n【学生笔记】\n%s" % (transcript or "（无）", note.strip() or "（无）")
    else:
        digests: List[Dict[str, Any]] = []
        for index, chunk in enumerate(_chunks(transcript, budget=budget // 2), start=1):
            try:
                digest = client.chat_json(
                    [
                        {"role": "system", "content": CHUNK_SYSTEM},
                        {"role": "user", "content": chunk},
                    ],
                    model=model,
                    temperature=0.2,
                    max_tokens=1200,
                    timeout=timeout,
                )
            except LLMError as exc:
                digests.append({"points": [], "_error": str(exc), "_chunk": index})
                continue
            digest["_chunk"] = index
            digests.append(digest)
        payload = "【分块摘要】\n%s\n\n【学生笔记】\n%s" % (
            json.dumps(digests, ensure_ascii=False),
            note.strip() or "（无）",
        )

    raw = client.chat_json(
        [
            {"role": "system", "content": SUMMARY_SYSTEM},
            {"role": "user", "content": payload},
        ],
        model=model,
        temperature=0.3,
        max_tokens=3000,
        timeout=timeout,
    )
    return normalize(raw)


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _str(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("text") or value.get("title") or "").strip()
    return str(value or "").strip()


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """把模型输出整理成稳定结构，字段缺失也不让前端崩。"""
    result: Dict[str, Any] = {
        "overview": _str(raw.get("overview") or raw.get("summary")),
        "points": [],
        "terms": [],
        "todos": [],
        "questions": [],
        "emphasis": [],
    }
    for item in _as_list(raw.get("points")):
        if isinstance(item, dict):
            heading = _str(item.get("heading") or item.get("title") or item.get("point"))
            detail = _str(item.get("detail") or item.get("description") or item.get("text"))
            if heading or detail:
                result["points"].append({"heading": heading or detail[:12], "detail": detail})
        else:
            text = _str(item)
            if text:
                result["points"].append({"heading": text[:12], "detail": text})

    seen = set()
    for item in _as_list(raw.get("terms")):
        if not isinstance(item, dict):
            continue
        term = _str(item.get("term") or item.get("word"))
        if not term or term.lower() in seen:
            continue
        seen.add(term.lower())
        result["terms"].append(
            {
                "term": term,
                "translation": _str(item.get("translation") or item.get("zh")),
                "note": _str(item.get("note") or item.get("explanation")),
            }
        )

    result["todos"] = [_str(x) for x in _as_list(raw.get("todos") or raw.get("assignments")) if _str(x)]
    result["questions"] = [_str(x) for x in _as_list(raw.get("questions")) if _str(x)]
    result["emphasis"] = [_str(x) for x in _as_list(raw.get("emphasis")) if _str(x)]
    return result
