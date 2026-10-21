"""分块：把长转写切成模型能一次吃完的片段，并保证上下文不断裂。

策略：按字符预算贪心装填，块与块之间保留若干句重叠，避免某个论点正好
落在切点上被两边都切掉。
"""

from __future__ import annotations

from typing import List

from .transcript import BilingualTurn


def render_turn(turn: BilingualTurn) -> str:
    """把一句双语内容渲染成便于模型阅读的两行。"""
    stamp = ""
    if turn.start is not None:
        total = int(turn.start)
        stamp = "[%02d:%02d] " % (total // 60, total % 60)
    lines = []
    if turn.source.strip():
        lines.append(stamp + turn.source.strip())
    if turn.target.strip():
        lines.append(("译：" if turn.source.strip() else stamp + "译：") + turn.target.strip())
    return "\n".join(lines)


def render_block(turns: List[BilingualTurn]) -> str:
    return "\n\n".join(render_turn(t) for t in turns)


def chunk_turns(
    turns: List[BilingualTurn],
    budget: int = 4000,
    overlap: int = 2,
) -> List[List[BilingualTurn]]:
    """按字符预算切块。

    ``budget`` 是每块的软上限（字符数），``overlap`` 是相邻块重叠的句数。
    """
    if budget <= 0:
        raise ValueError("budget 必须为正数")
    chunks: List[List[BilingualTurn]] = []
    current: List[BilingualTurn] = []
    size = 0

    for turn in turns:
        cost = len(turn.source) + len(turn.target) + 8
        if current and size + cost > budget:
            chunks.append(current)
            current = current[-overlap:] if overlap > 0 else []
            size = sum(len(t.source) + len(t.target) + 8 for t in current)
        current.append(turn)
        size += cost

    if current:
        chunks.append(current)
    return chunks


def stats(turns: List[BilingualTurn]) -> str:
    """给用户看的规模摘要。"""
    source_chars = sum(len(t.source) for t in turns)
    target_chars = sum(len(t.target) for t in turns)
    span = ""
    stamps = [t.start for t in turns if t.start is not None]
    if stamps:
        span = "，时长 %d 分钟" % max(1, int((max(stamps) - min(stamps)) / 60))
    return "%d 句，原文 %d 字符 / 译文 %d 字符%s" % (len(turns), source_chars, target_chars, span)
