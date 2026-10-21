"""渲染：把会话导出成一份能直接归档的 Markdown。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _ms(value: Any) -> str:
    try:
        total = int(value) // 1000
    except (TypeError, ValueError):
        return "00:00"
    return "%02d:%02d" % (total // 60, total % 60)


def _bullets(items: List[str], placeholder: str = "未提及。") -> List[str]:
    if not items:
        return ["_%s_" % placeholder]
    return ["- %s" % item for item in items]


def render_summary_sections(content: Dict[str, Any]) -> List[str]:
    """把结构化总结渲染成 Markdown 片段。字段缺失也不报错。"""
    lines: List[str] = []
    overview = (content.get("overview") or "").strip()
    if overview:
        lines.append("> %s" % overview.replace("\n", " "))
        lines.append("")

    points = content.get("points") or []
    if points:
        lines.append("### 要点提炼")
        lines.append("")
        for index, point in enumerate(points, start=1):
            if isinstance(point, dict):
                heading = (point.get("heading") or "").strip()
                detail = (point.get("detail") or "").strip()
                lines.append("%d. **%s**%s" % (index, heading, ("　" + detail) if detail else ""))
            else:
                lines.append("%d. %s" % (index, point))
        lines.append("")

    terms = content.get("terms") or []
    if terms:
        lines.append("### 关键术语")
        lines.append("")
        lines.append("| 原文 | 中文 | 说明 |")
        lines.append("| --- | --- | --- |")
        for term in terms:
            if not isinstance(term, dict):
                continue
            lines.append(
                "| %s | %s | %s |"
                % (
                    _cell(term.get("term")),
                    _cell(term.get("translation")),
                    _cell(term.get("note")),
                )
            )
        lines.append("")

    todos = content.get("todos") or []
    if todos:
        lines.append("### 待办")
        lines.append("")
        lines.extend(["- [ ] %s" % _text(item) for item in todos if _text(item)])
        lines.append("")

    questions = content.get("questions") or []
    if questions:
        lines.append("### 待查问题")
        lines.append("")
        lines.extend(["- %s" % _text(item) for item in questions if _text(item)])
        lines.append("")

    emphasis = content.get("emphasis") or []
    if emphasis:
        lines.append("### 老师强调 / 可能的考点")
        lines.append("")
        lines.extend(_bullets([_text(x) for x in emphasis if _text(x)]))
        lines.append("")
    return lines


def _text(value: Any) -> str:
    if isinstance(value, dict):
        return (value.get("text") or value.get("title") or "").strip()
    return str(value or "").strip()


def _cell(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ").strip()


def render_export(
    session: Dict[str, Any],
    note: str,
    summary: Optional[Dict[str, Any]],
    utterances: List[Dict[str, Any]],
) -> str:
    """导出结构：frontmatter → 总结 → 笔记 → 转写附录。"""
    lines: List[str] = []
    lines.append("---")
    lines.append('title: "%s"' % str(session.get("title", "")).replace('"', "'"))
    lines.append("date: %s" % str(session.get("created_at", ""))[:10])
    if session.get("course"):
        lines.append('course: "%s"' % str(session["course"]).replace('"', "'"))
    lines.append("language: %s" % session.get("source_lang", ""))
    lines.append("tags: [escucha, lecture-notes]")
    lines.append("---")
    lines.append("")
    lines.append("# %s" % session.get("title", "课堂记录"))
    lines.append("")

    if summary and summary.get("content"):
        lines.append("## 课堂总结")
        lines.append("")
        lines.extend(render_summary_sections(summary["content"]))
        lines.append(
            "<sub>总结生成于 %s · 模型 %s · 覆盖前 %s 句</sub>"
            % (str(summary.get("created_at", ""))[:16].replace("T", " "), summary.get("model") or "—", summary.get("covers_upto") or "—")
        )
        lines.append("")

    if note.strip():
        lines.append("## 课堂笔记")
        lines.append("")
        lines.append(note.strip())
        lines.append("")

    if utterances:
        lines.append("## 转写全文")
        lines.append("")
        for item in utterances:
            source = (item.get("source") or "").strip()
            target = (item.get("target") or "").strip()
            lines.append("**[%s]** %s" % (_ms(item.get("start_ms")), source))
            if target:
                lines.append("")
                lines.append("　%s" % target)
            lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("<sub>由 escucha 生成 · 共 %d 句</sub>" % len(utterances))
    lines.append("")
    return "\n".join(lines)
