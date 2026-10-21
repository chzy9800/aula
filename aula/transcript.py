"""转写文件解析：识别格式、切句、双语单调对齐。

支持的输入：
  * AirTranslate 导出的成对文件（``*_original.txt`` + ``*_translation.txt``）
  * SubRip (.srt)
  * WebVTT (.vtt)
  * 纯文本 (.txt / .md)

作者: apuntes contributors
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple

# 句末标点：拉丁 + 中日韩 + 省略号
_SENT_END = ".!?…。！？；;"
_LATIN_SENT_RE = re.compile(r"(?<=[.!?…])\s+(?=[\"'¿¡A-ZÁÉÍÓÚÜÑÄÖ])")
_CJK_SENT_RE = re.compile(r"(?<=[。！？；!?])")
# 常见西语缩写，切句时不该断开
_ABBREV = {
    "sr.", "sra.", "srta.", "dr.", "dra.", "ud.", "uds.", "etc.", "p.", "pp.",
    "ej.", "núm.", "aprox.", "vs.", "vol.", "cap.", "fig.", "art.", "n.º",
}

_SRT_TIME = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})\s*-->\s*(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
)


@dataclass
class Segment:
    """一段带可选时间戳的原文/译文。"""

    text: str
    start: Optional[float] = None
    end: Optional[float] = None

    def stamp(self) -> str:
        """返回 ``[mm:ss]`` 形式的时间戳，没有时间戳时返回空串。"""
        if self.start is None:
            return ""
        total = int(self.start)
        return "[%02d:%02d]" % (total // 60, total % 60)


@dataclass
class BilingualTurn:
    """对齐后的一句双语内容。"""

    source: str
    target: str = ""
    start: Optional[float] = None

    @property
    def empty(self) -> bool:
        return not (self.source.strip() or self.target.strip())


@dataclass
class Document:
    """一份完整转写。``source_lang`` 是原文语言，``target_lang`` 是译文语言。"""

    segments: List[Segment] = field(default_factory=list)
    source_lang: str = "auto"
    target_lang: str = "zh"
    origin: str = ""

    def text(self) -> str:
        return "\n".join(s.text for s in self.segments if s.text.strip())

    def __len__(self) -> int:
        return len(self.segments)


def _has_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def _looks_spanish(text: str) -> bool:
    lowered = text.lower()
    hits = sum(lowered.count(w) for w in (" que ", " de la ", " el ", " los ", " para ", " con "))
    return hits >= 3


def guess_lang(text: str) -> str:
    """粗粒度语言判断，仅用于选择切句策略和写 frontmatter。

    西语优先看变音符号（á é í ó ú ñ ¿ ¡ ü）—— 这个特征比统计停用词可靠得多，
    短句也能判对。
    """
    head = text[:4000]
    if _has_cjk(head):
        return "zh"
    if re.search(r"[áéíóúñ¿¡ü]", head.lower()):
        return "es"
    lowered = " " + head.lower() + " "
    hits = sum(
        len(re.findall(r"\b%s\b" % word, lowered))
        for word in ("que", "de", "la", "el", "los", "las", "para", "con", "una", "por")
    )
    return "es" if hits >= 4 else "en"


def split_sentences(text: str, lang: str = "auto") -> List[str]:
    """把整段文本切成句子，保留原始顺序。

    规则刻意保守：宁可切得少，也不要把一句话拆坏。缩写（``Sr.``、``etc.``）
    不会被当作句末。
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    if lang == "auto":
        lang = guess_lang(text)

    chunks: List[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        if lang == "zh" or _has_cjk(line):
            parts = _CJK_SENT_RE.split(line)
        else:
            parts = _LATIN_SENT_RE.split(line)
        for part in parts:
            part = part.strip()
            if part:
                chunks.append(part)

    return _merge_abbrev_fragments(chunks)


def _merge_abbrev_fragments(chunks: List[str]) -> List[str]:
    """把被缩写误切的碎片合回上一句。"""
    out: List[str] = []
    for chunk in chunks:
        if out:
            tail = out[-1].split()[-1].lower() if out[-1].split() else ""
            if tail in _ABBREV:
                out[-1] = out[-1] + " " + chunk
                continue
            # 太短的碎片（<4 字符）多半是误切，粘回去
            if len(chunk) < 4 and out[-1].endswith(tuple(_SENT_END)):
                out[-1] = out[-1] + " " + chunk
                continue
        out.append(chunk)
    return out


def _time_to_seconds(h: str, m: str, s: str, ms: str) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000.0


def parse_timed(text: str) -> List[Segment]:
    """解析 .srt / .vtt：按时间轴块提取字幕行。"""
    text = text.replace("\r\n", "\n")
    segments: List[Segment] = []
    blocks = re.split(r"\n\s*\n", text)
    for block in blocks:
        lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        if lines[0].upper().startswith("WEBVTT"):
            lines = lines[1:]
            if not lines:
                continue
        start = end = None
        body_start = 0
        matched = False
        for i, line in enumerate(lines):
            match = _SRT_TIME.search(line)
            if match:
                g = match.groups()
                start = _time_to_seconds(*g[:4])
                end = _time_to_seconds(*g[4:])
                body_start = i + 1
                matched = True
                break
        if not matched:
            # 没有时间轴的块不算字幕，交给纯文本解析器处理
            continue
        body = " ".join(lines[body_start:]).strip()
        body = re.sub(r"<[^>]+>", "", body)  # 去掉 vtt 内联标签
        if body:
            segments.append(Segment(text=body, start=start, end=end))
    return segments


def parse_plain(text: str, lang: str = "auto") -> List[Segment]:
    """纯文本：只切句，不带时间轴。"""
    return [Segment(text=s) for s in split_sentences(text, lang)]


def load(path: Path, lang: str = "auto") -> Document:
    """按扩展名/内容自动选择解析器。"""
    raw = path.read_text(encoding="utf-8", errors="replace")
    lower = path.name.lower()
    segments: List[Segment] = []
    if lower.endswith((".srt", ".vtt")) or "-->" in raw[:2000]:
        segments = parse_timed(raw)
    if not segments:
        segments = parse_plain(raw, lang)
    return Document(
        segments=segments,
        source_lang=guess_lang(raw) if lang == "auto" else lang,
        origin=str(path),
    )


def find_pairs(directory: Path) -> List[Tuple[Path, Path]]:
    """在目录中找出 AirTranslate 风格的成对文件（original + translation）。"""
    originals = sorted(directory.glob("*_original.txt"))
    pairs: List[Tuple[Path, Path]] = []
    for original in originals:
        translation = original.with_name(original.name.replace("_original.txt", "_translation.txt"))
        if translation.exists():
            pairs.append((original, translation))
    return pairs


def align_monotonic(left: List[str], right: List[str]) -> List[Tuple[str, str]]:
    """把两组句子按长度比例单调对齐。

    两种语言切句数几乎不可能一致（译者会拆分或合并）。这里用归一化累计
    长度做比例映射，保证输出严格单调、不交叉，并且不丢任何一句。
    """
    if not left and not right:
        return []
    if not left:
        return [("", r) for r in right]
    if not right:
        return [(l, "") for l in left]
    if len(left) == len(right):
        return list(zip(left, right))

    def cumulative(items: List[str]) -> List[float]:
        running = 0.0
        marks = []
        total = float(sum(max(len(x), 1) for x in items))
        for item in items:
            running += max(len(item), 1)
            marks.append(running / total)
        return marks

    left_marks = cumulative(left)
    right_marks = cumulative(right)

    pairs: List[Tuple[str, str]] = []
    left_index = 0
    for right_index, right_item in enumerate(right):
        mid = (right_marks[right_index] + (right_marks[right_index - 1] if right_index else 0.0)) / 2.0
        while left_index < len(left) - 1 and left_marks[left_index] < mid:
            left_index += 1
        # 上一句还有剩余译文时合并到同一句，避免出现空行
        if pairs and pairs[-1][0] == left[left_index]:
            previous = pairs[-1][1]
            glue = " " if (previous and right_item and previous[-1].isascii() and right_item[0].isascii()) else ""
            pairs[-1] = (pairs[-1][0], previous + glue + right_item)
        else:
            pairs.append((left[left_index], right_item))
    return pairs


def load_bilingual(original: Path, translation: Path) -> List[BilingualTurn]:
    """加载一份成对转写并对齐成双语序列。"""
    src = load(original)
    dst = load(translation)
    aligned = align_monotonic(
        [s.text for s in src.segments],
        [s.text for s in dst.segments],
    )
    turns: List[BilingualTurn] = []
    for i, (left, right) in enumerate(aligned):
        start = src.segments[i].start if i < len(src.segments) else None
        turns.append(BilingualTurn(source=left, target=right, start=start))
    return turns
