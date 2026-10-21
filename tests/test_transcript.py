"""解析与对齐的测试。

对齐是这个项目里最容易悄悄出错的地方：句子数量不匹配时如果实现成了
"拉链式"配对，就会丢句或串行，而输出看起来仍然"像样"。所以这里重点
验证不丢句、严格单调这两条不变量。
"""

import sys
import unittest
from pathlib import Path



from aula.transcript import (  # noqa: E402
    Segment,
    align_monotonic,
    find_pairs,
    guess_lang,
    load,
    parse_timed,
    split_sentences,
)


class SplitSentencesTest(unittest.TestCase):
    def test_spanish_sentences(self):
        text = "Hola a todos. Vamos a ver la instalación eléctrica. ¿Empezamos?"
        parts = split_sentences(text, "es")
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[1].startswith("Vamos"))

    def test_chinese_sentences(self):
        text = "首先是总体施工计划。然后是人力资源分配！最后是材料储备？"
        parts = split_sentences(text, "zh")
        self.assertEqual(len(parts), 3)
        self.assertTrue(parts[0].endswith("。"))

    def test_abbreviation_not_split(self):
        text = "El Sr. García llegó tarde. Después empezó la clase."
        parts = split_sentences(text, "es")
        self.assertEqual(len(parts), 2)
        self.assertIn("Sr. García", parts[0])

    def test_newlines_are_boundaries(self):
        text = "第一句。\n第二句。\n\n第三句。"
        parts = split_sentences(text, "zh")
        self.assertEqual(len(parts), 3)

    def test_guess_lang(self):
        self.assertEqual(guess_lang("这是一段中文内容，包含很多汉字。"), "zh")
        self.assertEqual(guess_lang("Vamos a ver que el plan de la instalación con los documentos"), "es")


class TimedParseTest(unittest.TestCase):
    def test_srt(self):
        content = (
            "1\n00:00:01,000 --> 00:00:04,500\nHola a todos.\n\n"
            "2\n00:00:05,000 --> 00:00:07,000\nEmpezamos la clase.\n"
        )
        segments = parse_timed(content)
        self.assertEqual(len(segments), 2)
        self.assertAlmostEqual(segments[0].start, 1.0)
        self.assertAlmostEqual(segments[0].end, 4.5)
        self.assertEqual(segments[0].stamp(), "[00:01]")

    def test_vtt_strips_tags(self):
        content = "WEBVTT\n\n00:00:02.000 --> 00:00:03.000\n<c.yellow>Hola</c> a todos.\n"
        segments = parse_timed(content)
        self.assertEqual(len(segments), 1)
        self.assertNotIn("<c.", segments[0].text)
        self.assertIn("Hola", segments[0].text)

    def test_plain_text_loaded_as_segments(self):
        segments = parse_timed("no timestamps here\n")
        self.assertEqual(segments, [])


class AlignTest(unittest.TestCase):
    def test_equal_length_is_zip(self):
        pairs = align_monotonic(["a", "b"], ["甲", "乙"])
        self.assertEqual(pairs, [("a", "甲"), ("b", "乙")])

    def test_no_sentence_dropped(self):
        left = ["a" * 40, "b" * 60, "c" * 30]
        right = ["甲" * 10, "乙" * 12, "丙" * 11, "丁" * 9, "戊" * 10]
        pairs = align_monotonic(left, right)
        used_left = {p[0] for p in pairs}
        used_right = "".join(p[1] for p in pairs)
        self.assertEqual(used_left, set(left))
        self.assertEqual(used_right, "".join(right))

    def test_monotonic_order(self):
        left = [str(i) * 10 for i in range(1, 6)]
        right = [chr(0x4E00 + i) * 8 for i in range(12)]
        pairs = align_monotonic(left, right)
        indexes = [int(p[0][0]) for p in pairs]
        self.assertEqual(indexes, sorted(indexes), "对齐结果必须单调不交叉")

    def test_empty_sides(self):
        self.assertEqual(align_monotonic([], []), [])
        self.assertEqual(align_monotonic([], ["甲"]), [("", "甲")])
        self.assertEqual(align_monotonic(["a"], []), [("a", "")])


class PairDiscoveryTest(unittest.TestCase):
    def test_find_pairs(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            (directory / "2026-10-18_x_original.txt").write_text("Hola.", encoding="utf-8")
            (directory / "2026-10-18_x_translation.txt").write_text("你好。", encoding="utf-8")
            (directory / "2026-10-18_y_original.txt").write_text("Sin par.", encoding="utf-8")
            pairs = find_pairs(directory)
            self.assertEqual(len(pairs), 1)
            self.assertTrue(pairs[0][0].name.endswith("_original.txt"))
            self.assertTrue(pairs[0][1].name.endswith("_translation.txt"))

    def test_load_srt_file(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "a.srt"
            path.write_text("1\n00:00:01,000 --> 00:00:02,000\nHola.\n", encoding="utf-8")
            document = load(path)
            self.assertEqual(len(document.segments), 1)
            self.assertIsInstance(document.segments[0], Segment)


if __name__ == "__main__":
    unittest.main()
