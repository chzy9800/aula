"""两级归纳：先逐块抽取，再全局合成听课记录。

为什么不直接一次性喂完整转写？一节 90 分钟的课转写下来两三万字，塞进
一次请求既贵又容易"读到后面忘了前面"，模型会开始概括性胡说。分块抽取
只要求模型"从这段里挑事实"，全局合成只要求它"把这些事实组织成结构"——
两件事都比"一次读懂全部"可靠。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional

from .chunk import chunk_turns, render_block
from .llm import LLMClient, LLMError
from .transcript import BilingualTurn

CHUNK_SYSTEM = """你是一名严谨的课堂速记员。用户给你一段课堂转写，通常包含外语原文和中文译文。

你的任务是**只抽取**，不做发挥。输出一个 JSON 对象，字段如下：

{
  "points": ["这一段的要点，一句话一条，中文书写；涉及外文术语时写「术语（中文）」"],
  "terms": [{"term": "外文原词", "translation": "中文译名", "note": "一句话解释"}],
  "emphasis": ["老师明确强调、重复、或提示会考的内容"],
  "assignments": ["布置的作业、阅读、提交要求"],
  "questions": ["这段里没讲清、值得课后自己查的点"]
}

硬性要求：
1. 只写转写里真实出现的内容。没有就留空数组，绝不编造。
2. points 控制在 3-8 条之间，抓主干，不要逐句复述。
3. term 必须是转写中出现的外文原词，不要自己翻译回去造词。
4. 转写可能存在语音识别错误，明显的同音错字可按上下文修正；不确定的原样保留。
5. 只输出 JSON，不要任何解释文字。"""

SYNTH_SYSTEM = """你是一名把课堂记录整理成复习资料的学习助手。用户会给你若干个 JSON 片段，每个片段是一段课堂转写抽取出的要点、术语、强调、作业与疑问。

请把它们合并成一份完整的听课记录，输出 JSON：

{
  "title": "本节课主题（不超过 20 字，具体到知识点，不要写「关于……的课程」）",
  "summary": "2-3 句话的整节课概述",
  "points": [{"heading": "要点小标题（不超过 12 字）", "detail": "2-4 句展开，讲清逻辑关系"}],
  "terms": [{"term": "外文原词", "translation": "中文译名", "note": "一句话解释"}],
  "emphasis": ["老师强调 / 可能的考点"],
  "assignments": ["作业与下次预告"],
  "questions": ["建议课后自查的问题，具体可执行"]
}

硬性要求：
1. 合并重复内容，同一条要点只出现一次；片段之间重叠的部分不要重复罗列。
2. 保持课堂原有的推进顺序，不要按自己的逻辑重排。
3. points 数量 4-8 条；每条 detail 要有信息量，不要写「老师讲解了……」这种空话。
4. 所有字段都必须存在。没有内容的字段用空数组；questions 至少给 2 条。
5. 不要编造片段里没有的内容。宁可留空。
6. 只输出 JSON，不要任何解释文字。"""


@dataclass
class KeyTerm:
    term: str
    translation: str = ""
    note: str = ""


@dataclass
class Point:
    heading: str
    detail: str = ""


@dataclass
class LectureNotes:
    """最终产出的听课记录。"""

    title: str = "未命名课堂"
    summary: str = ""
    points: List[Point] = field(default_factory=list)
    terms: List[KeyTerm] = field(default_factory=list)
    emphasis: List[str] = field(default_factory=list)
    assignments: List[str] = field(default_factory=list)
    questions: List[str] = field(default_factory=list)
    chunks: int = 0
    source_chars: int = 0
    meta: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        return data


def digest_chunks(
    turns: List[BilingualTurn],
    client: LLMClient,
    chunk_budget: int = 4000,
    overlap: int = 2,
    progress: Optional[Callable[[str], None]] = None,
) -> List[Dict[str, Any]]:
    """逐块抽取。某块失败会记录 warning 并继续，不让整节课报废。"""
    chunks = chunk_turns(turns, budget=chunk_budget, overlap=overlap)
    say = progress or (lambda msg: None)
    digests: List[Dict[str, Any]] = []

    for index, chunk in enumerate(chunks, start=1):
        say("抽取第 %d/%d 块（%d 句）" % (index, len(chunks), len(chunk)))
        message = [
            {"role": "system", "content": CHUNK_SYSTEM},
            {"role": "user", "content": render_block(chunk)},
        ]
        try:
            digest = client.chat_json(message)
        except LLMError as exc:
            say("  跳过：%s" % str(exc).splitlines()[0])
            digests.append({"points": [], "terms": [], "emphasis": [], "assignments": [], "questions": [], "_error": str(exc)})
            continue
        digest["_chunk"] = index
        digests.append(digest)
    return digests


def synthesize(
    digests: List[Dict[str, Any]],
    client: LLMClient,
    progress: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """把各块抽取结果合成整节课的记录。"""
    say = progress or (lambda msg: None)
    payload = json.dumps(digests, ensure_ascii=False)
    # 片段太多时也要避免撑爆上下文：只保留有内容的字段
    if len(payload) > 24000:
        payload = payload[:24000] + "\n[后续片段因长度截断]"
    say("合成为听课记录（%d 个片段）" % len(digests))
    message = [
        {"role": "system", "content": SYNTH_SYSTEM},
        {"role": "user", "content": payload},
    ]
    return client.chat_json(message)


def build_notes(
    turns: List[BilingualTurn],
    client: LLMClient,
    chunk_budget: int = 4000,
    overlap: int = 2,
    progress: Optional[Callable[[str], None]] = None,
) -> LectureNotes:
    """完整流程：分块抽取 → 全局合成 → 规范化成 LectureNotes。"""
    digests = digest_chunks(turns, client, chunk_budget=chunk_budget, overlap=overlap, progress=progress)
    if not any(d.get("points") for d in digests):
        raise LLMError("所有分块都没有抽取到内容，检查模型是否可用")
    raw = synthesize(digests, client, progress=progress)
    notes = normalize(raw)
    notes.chunks = len(digests)
    notes.source_chars = sum(len(t.source) + len(t.target) for t in turns)
    return notes


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        return [value] if value.strip() else []
    return [value]


def _as_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def normalize(raw: Dict[str, Any]) -> LectureNotes:
    """把模型返回的任意形状整理成稳定结构。"""
    notes = LectureNotes(
        title=_as_str(raw.get("title")) or "未命名课堂",
        summary=_as_str(raw.get("summary")),
    )

    for item in _as_list(raw.get("points")):
        if isinstance(item, dict):
            heading = _as_str(item.get("heading") or item.get("title") or item.get("point"))
            detail = _as_str(item.get("detail") or item.get("description") or item.get("text"))
            if heading or detail:
                notes.points.append(Point(heading=heading or detail[:12], detail=detail))
        else:
            text = _as_str(item)
            if text:
                notes.points.append(Point(heading=text[:12], detail=text))

    seen = set()
    for item in _as_list(raw.get("terms")):
        if not isinstance(item, dict):
            continue
        term = _as_str(item.get("term") or item.get("word"))
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        notes.terms.append(
            KeyTerm(
                term=term,
                translation=_as_str(item.get("translation") or item.get("zh")),
                note=_as_str(item.get("note") or item.get("explanation")),
            )
        )

    notes.emphasis = [s for s in (_as_str(x) for x in _as_list(raw.get("emphasis"))) if s]
    notes.assignments = [s for s in (_as_str(x) for x in _as_list(raw.get("assignments"))) if s]
    notes.questions = [s for s in (_as_str(x) for x in _as_list(raw.get("questions"))) if s]
    return notes
