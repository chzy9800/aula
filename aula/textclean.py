"""清洗：去掉口头填充词、合并碎句、压制 ASR 重复。

转写文本的三个典型病症：
  1. 填充词泛滥（"嗯…呃…este…bueno…"），占掉大量 token 却不含信息
  2. 一句话被切得七零八落，读起来断气
  3. 识别循环导致同一句连续出现多次

这里做的都是保守清洗：只删明确无信息的部分，不改写措辞。
"""

from __future__ import annotations

import re
from typing import List

from .transcript import BilingualTurn

# 出现在句首/句尾/句中单独成词时才算填充词。
# 注意：不放「这个」「那个」——它们在中文里常是真正的指示代词，删了会伤内容。
_CJK_FILLERS = ["嗯", "呃", "啊", "唉", "诶", "哦", "就是说", "然后呢"]
_ES_FILLERS = [
    "eh", "em", "mmm", "este", "esto", "bueno", "pues", "o sea", "digamos",
    "¿vale?", "¿sí?", "¿no?", "verdad", "en fin",
]

_CJK_FILLER_RE = re.compile(
    r"(^|[，。！？、；：\s])(" + "|".join(map(re.escape, _CJK_FILLERS)) + r")(?=[，。！？、；：\s]|$)"
)
_ES_FILLER_RE = re.compile(
    r"(?<![A-Za-zÁÉÍÓÚÜÑáéíóúüñ])(" + "|".join(map(re.escape, _ES_FILLERS)) + r")(?![A-Za-zÁÉÍÓÚÜÑáéíóúüñ])",
    re.IGNORECASE,
)
_SPACE_RE = re.compile(r"[ \t]{2,}")
_PUNCT_RE = re.compile(r"([，。！？；：、])\1+")


def clean_text(text: str, lang: str = "auto") -> str:
    """清洗单段文本。"""
    if not text:
        return ""
    original = text
    text = _ES_FILLER_RE.sub("", text)
    text = _CJK_FILLER_RE.sub(r"\1", text)
    text = _PUNCT_RE.sub(r"\1", text)
    text = _SPACE_RE.sub(" ", text)
    # 清掉填充词后可能留下孤零零的标点（中英文标点都要管）
    text = re.sub(r"^[，。；：、,;:\s]+", "", text)
    text = re.sub(r"[，、,;:\s]+$", "", text)
    text = text.strip()
    return text or original.strip()


def merge_short_turns(turns: List[BilingualTurn], min_chars: int = 12) -> List[BilingualTurn]:
    """把过短的碎句并入下一句，减少条目数量。"""
    merged: List[BilingualTurn] = []
    pending: List[BilingualTurn] = []
    for turn in turns:
        if not turn.source.strip() and not turn.target.strip():
            continue
        pending.append(turn)
        length = sum(len(t.target) + len(t.source) for t in pending)
        if length >= min_chars:
            merged.append(_join(pending))
            pending = []
    if pending:
        if merged:
            merged[-1] = _join([merged[-1]] + pending)
        else:
            merged.append(_join(pending))
    return merged


def _join(turns: List[BilingualTurn]) -> BilingualTurn:
    start = next((t.start for t in turns if t.start is not None), None)

    def stitch(parts: List[str]) -> str:
        joined = ""
        for part in parts:
            if not part:
                continue
            if joined and joined[-1].isascii() and part[0].isascii():
                joined += " "
            joined += part
        return joined

    return BilingualTurn(
        source=stitch([t.source.strip() for t in turns if t.source.strip()]),
        target=stitch([t.target.strip() for t in turns if t.target.strip()]),
        start=start,
    )


def dedupe(turns: List[BilingualTurn]) -> List[BilingualTurn]:
    """去掉 ASR 循环产生的连续重复句。"""
    out: List[BilingualTurn] = []
    for turn in turns:
        if out and turn.target.strip() and turn.target.strip() == out[-1].target.strip():
            continue
        out.append(turn)
    return out


def clean_turns(turns: List[BilingualTurn], source_lang: str = "auto") -> List[BilingualTurn]:
    """完整清洗流程：去填充词 → 去重 → 合并碎句。"""
    cleaned = [
        BilingualTurn(
            source=clean_text(t.source, source_lang),
            target=clean_text(t.target, "zh"),
            start=t.start,
        )
        for t in turns
    ]
    return merge_short_turns(dedupe([t for t in cleaned if not t.empty]))
