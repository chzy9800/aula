"""分块测试：预算、重叠、不丢句。"""

import sys
import unittest
from pathlib import Path



from aula.chunk import chunk_turns, render_block, stats  # noqa: E402
from aula.transcript import BilingualTurn  # noqa: E402


def make_turns(count: int, size: int = 40):
    return [
        BilingualTurn(source="x" * size, target="y" * size, start=float(i * 5))
        for i in range(count)
    ]


class ChunkTest(unittest.TestCase):
    def test_single_chunk_when_small(self):
        chunks = chunk_turns(make_turns(3), budget=10000)
        self.assertEqual(len(chunks), 1)

    def test_budget_respected(self):
        chunks = chunk_turns(make_turns(20), budget=800, overlap=0)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            size = sum(len(t.source) + len(t.target) for t in chunk)
            self.assertLessEqual(size, 800 + 88, "单块不应远超预算")

    def test_every_turn_present(self):
        turns = make_turns(15)
        chunks = chunk_turns(turns, budget=500, overlap=0)
        collected = [t for chunk in chunks for t in chunk]
        self.assertEqual(len(collected), len(turns))

    def test_overlap_repeats_boundary(self):
        turns = make_turns(15)
        chunks = chunk_turns(turns, budget=500, overlap=2)
        if len(chunks) > 1:
            tail = chunks[0][-2:]
            self.assertEqual([t.source for t in tail], [t.source for t in chunks[1][:2]])

    def test_invalid_budget(self):
        with self.assertRaises(ValueError):
            chunk_turns(make_turns(2), budget=0)


class RenderTest(unittest.TestCase):
    def test_timestamp_rendered(self):
        block = render_block([BilingualTurn("Hola", "你好", start=125.0)])
        self.assertIn("[02:05] Hola", block)
        self.assertIn("译：你好", block)

    def test_stats_counts(self):
        text = stats(make_turns(2, size=10))
        self.assertIn("2 句", text)


if __name__ == "__main__":
    unittest.main()
