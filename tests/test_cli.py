"""CLI 与渲染测试：跑通 preview、验证 Markdown 结构。"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path



from aula.cli import _resolve_out, main  # noqa: E402
from aula.digest import KeyTerm, LectureNotes, Point  # noqa: E402
from aula.doc_render import render_markdown, safe_filename  # noqa: E402


def sample_notes() -> LectureNotes:
    return LectureNotes(
        title="电气装置施工计划",
        summary="讲解总体施工计划的六个组成部分。",
        points=[Point("总体计划", "包含全部技术文件。"), Point("装配计划", "明确每一步的顺序与工期。")],
        terms=[KeyTerm("plan general", "总体计划", "施工总纲")],
        emphasis=["工期控制是考点"],
        assignments=["阅读 UD4 材料"],
        questions=["五周工期怎么拆分？", "风险预防如何贯穿全程？"],
        chunks=2,
        source_chars=8800,
    )


class RenderTest(unittest.TestCase):
    def test_frontmatter_and_sections(self):
        markdown = render_markdown(sample_notes(), date="2026-10-18", language="es", model="deepseek-chat")
        self.assertTrue(markdown.startswith("---\n"))
        for heading in ("## 核心要点", "## 关键术语", "## 老师强调 / 可能的考点", "## 作业与下次预告", "## 待自查问题"):
            self.assertIn(heading, markdown)
        self.assertIn("| plan general | 总体计划 | 施工总纲 |", markdown)
        self.assertIn("- [ ] 五周工期怎么拆分？", markdown)
        self.assertIn("deepseek-chat", markdown)

    def test_empty_sections_have_placeholder(self):
        markdown = render_markdown(LectureNotes(title="空课"))
        self.assertIn("_未提取到要点。_", markdown)
        self.assertIn("_未提及。_", markdown)

    def test_pipe_escaped_in_table(self):
        notes = LectureNotes(title="x", terms=[KeyTerm("a|b", "甲", "含|竖线")])
        markdown = render_markdown(notes)
        self.assertIn("a\\|b", markdown)

    def test_yaml_quote_escaped(self):
        notes = LectureNotes(title='包含 "引号": 与冒号')
        markdown = render_markdown(notes)
        self.assertIn('title: "包含 \'引号\': 与冒号"', markdown)

    def test_safe_filename(self):
        self.assertEqual(safe_filename("施工/计划", "2026-10-18"), "2026-10-18-施工计划.md")
        self.assertTrue(safe_filename("", "2026-10-18").endswith(".md"))


class CliTest(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        err = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_preview_on_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original = directory / "2026-10-18_x_original.txt"
            translation = directory / "2026-10-18_x_translation.txt"
            original.write_text("Hola a todos. Vamos a ver el plan general. Después el plazo.", encoding="utf-8")
            translation.write_text("大家好。我们来看总体计划。然后是工期。", encoding="utf-8")
            code, out, _ = self.run_cli(["preview", str(original)])
            self.assertEqual(code, 0)
            self.assertIn("语言：es", out)
            self.assertIn("分块：1 块", out)
            self.assertIn("Hola a todos", out)

    def test_preview_missing_file(self):
        code, _, err = self.run_cli(["preview", "/nonexistent/file.txt"])
        self.assertEqual(code, 1)
        self.assertIn("找不到文件", err)

    def test_preview_single_language_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notas.txt"
            path.write_text("第一句。第二句。", encoding="utf-8")
            code, out, _ = self.run_cli(["preview", str(path)])
            self.assertEqual(code, 0)
            self.assertIn("语言：zh", out)


class ResolveOutTest(unittest.TestCase):
    def test_directory_without_trailing_slash(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "notes"
            path = _resolve_out(str(target), "标题", "2026-10-18", Path("/tmp/x_original.txt"))
            self.assertEqual(path, target / "2026-10-18-标题.md")

    def test_explicit_markdown_file(self):
        path = _resolve_out("/tmp/mine.md", "标题", "2026-10-18", Path("/tmp/x_original.txt"))
        self.assertEqual(str(path), "/tmp/mine.md")

    def test_default_is_next_to_input(self):
        path = _resolve_out(None, "标题", "2026-10-18", Path("/tmp/dir/x_original.txt"))
        self.assertEqual(path, Path("/tmp/dir/2026-10-18-标题.md"))


if __name__ == "__main__":
    unittest.main()
