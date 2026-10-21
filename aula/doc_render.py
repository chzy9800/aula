"""渲染：把 LectureNotes 变成一份能直接归档的 Markdown。

格式选择 Obsidian / Logseq 友好的写法：frontmatter 用列表而非对象嵌套，
标签扁平，小节固定。这样生成的笔记能直接被 Dataview 之类的插件检索。
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from .digest import LectureNotes
from .transcript import BilingualTurn


def _yaml_str(value: str) -> str:
    """给 frontmatter 值加引号，避免冒号、井号把 YAML 炸掉。"""
    text = (value or "").replace('"', "'").replace("\n", " ").strip()
    return '"%s"' % text


def render_markdown(
    notes: LectureNotes,
    date: str = "",
    course: str = "",
    source: str = "",
    language: str = "auto",
    model: str = "",
) -> str:
    """生成完整听课记录。"""
    date = date or datetime.now().strftime("%Y-%m-%d")
    tags = ["apuntes", "lecture-notes"]
    if language and language != "auto":
        tags.append("lang-%s" % language)

    lines: List[str] = []
    lines.append("---")
    lines.append("title: %s" % _yaml_str(notes.title))
    lines.append("date: %s" % date)
    if course:
        lines.append("course: %s" % _yaml_str(course))
    if source:
        lines.append("source: %s" % _yaml_str(source))
    lines.append("language: %s" % language)
    lines.append("tags: [%s]" % ", ".join(tags))
    lines.append("---")
    lines.append("")
    lines.append("# %s" % notes.title)
    lines.append("")

    if notes.summary:
        lines.append("> %s" % notes.summary.replace("\n", " "))
        lines.append("")

    lines.append("## 核心要点")
    lines.append("")
    if notes.points:
        for index, point in enumerate(notes.points, start=1):
            heading = point.heading or "要点 %d" % index
            lines.append("### %d. %s" % (index, heading))
            if point.detail and point.detail != heading:
                lines.append("")
                lines.append(point.detail)
            lines.append("")
    else:
        lines.append("_未提取到要点。_")
        lines.append("")

    lines.append("## 关键术语")
    lines.append("")
    if notes.terms:
        lines.append("| 原文 | 中文 | 说明 |")
        lines.append("| --- | --- | --- |")
        for term in notes.terms:
            lines.append("| %s | %s | %s |" % (_cell(term.term), _cell(term.translation), _cell(term.note)))
    else:
        lines.append("_本课未记录到外文术语。_")
    lines.append("")

    lines.append("## 老师强调 / 可能的考点")
    lines.append("")
    lines.extend(_bullets(notes.emphasis, "未明确提及。"))
    lines.append("")

    lines.append("## 作业与下次预告")
    lines.append("")
    lines.extend(_bullets(notes.assignments, "未提及。"))
    lines.append("")

    lines.append("## 待自查问题")
    lines.append("")
    if notes.questions:
        lines.extend(["- [ ] %s" % q for q in notes.questions])
    else:
        lines.append("_模型未给出。建议回看要点里不熟悉的部分。_")
    lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("<sub>由 apuntes 生成 · %s · 分块 %d 个 · 转写 %d 字符%s</sub>" % (
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        notes.chunks,
        notes.source_chars,
        (" · 模型 %s" % model) if model else "",
    ))
    lines.append("")
    return "\n".join(lines)


def _cell(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ").strip()


def _bullets(items: List[str], placeholder: str) -> List[str]:
    if not items:
        return ["_%s_" % placeholder]
    return ["- %s" % item for item in items]


def render_appendix(turns: List[BilingualTurn], limit: int = 0) -> str:
    """可选：把清洗后的双语原文附在末尾，方便对照。"""
    from .chunk import render_turn

    selected = turns[:limit] if limit else turns
    body = "\n\n".join(render_turn(t) for t in selected)
    return "\n## 附：双语原文\n\n<details>\n<summary>展开对照原文</summary>\n\n%s\n\n</details>\n" % body


def safe_filename(title: str, date: str, fallback: str = "lecture") -> str:
    """把标题变成安全的文件名。"""
    cleaned = "".join(ch for ch in title if ch not in '\\/:*?"<>|\n\r\t').strip()
    cleaned = cleaned.replace(" ", "-")[:60] or fallback
    return "%s-%s.md" % (date, cleaned)
