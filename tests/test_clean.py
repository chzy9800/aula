"""清洗逻辑测试：删干净但不删过头。"""

import sys
import unittest
from pathlib import Path



from aula.textclean import clean_text, clean_turns, dedupe, merge_short_turns  # noqa: E402
from aula.transcript import BilingualTurn  # noqa: E402


class CleanTextTest(unittest.TestCase):
    def test_chinese_fillers_removed(self):
        # 「这个」是指示代词，保留；只删纯语气词
        self.assertEqual(clean_text("嗯，这个就是总体施工计划。", "zh"), "这个就是总体施工计划。")

    def test_spanish_fillers_removed(self):
        cleaned = clean_text("Bueno, este es el plan general de construcción.", "es")
        self.assertNotIn("Bueno", cleaned)
        self.assertIn("plan general", cleaned)

    def test_leading_comma_removed(self):
        self.assertEqual(clean_text(", la organización del trabajo", "es"), "la organización del trabajo")

    def test_repeated_punctuation_collapsed(self):
        self.assertEqual(clean_text("好的。。。。。", "zh"), "好的。")

    def test_empty_input(self):
        self.assertEqual(clean_text("", "zh"), "")

    def test_all_filler_keeps_original(self):
        # 整句都是填充词时不能清成空串，否则会丢信息
        self.assertTrue(clean_text("嗯。", "zh"))


class DedupeTest(unittest.TestCase):
    def test_consecutive_duplicates_removed(self):
        turns = [
            BilingualTurn("Hola", "你好"),
            BilingualTurn("Hola", "你好"),
            BilingualTurn("Adiós", "再见"),
        ]
        self.assertEqual(len(dedupe(turns)), 2)


class MergeShortTest(unittest.TestCase):
    def test_short_turns_merged(self):
        turns = [BilingualTurn("Sí", "是"), BilingualTurn("No", "否"), BilingualTurn("Vale", "好的")]
        merged = merge_short_turns(turns, min_chars=12)
        self.assertLess(len(merged), len(turns))

    def test_timestamps_preserved(self):
        turns = [BilingualTurn("Sí", "是", start=12.0), BilingualTurn("claro que sí, vamos", "当然，走吧", start=14.0)]
        merged = merge_short_turns(turns, min_chars=30)
        self.assertEqual(merged[0].start, 12.0)


class CleanTurnsTest(unittest.TestCase):
    def test_pipeline_drops_empty(self):
        turns = [BilingualTurn("", ""), BilingualTurn("Vamos a ver el plan", "我们来看计划")]
        cleaned = clean_turns(turns, "es")
        self.assertEqual(len(cleaned), 1)
        self.assertIn("plan", cleaned[0].source)


if __name__ == "__main__":
    unittest.main()
