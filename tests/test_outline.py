"""大纲生成测试：页标记、分块、合并去重、接口行为。"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from aula import config, outline, reader, server  # noqa: E402
from aula.llm import LLMError  # noqa: E402


PAGES = [
    {"index": 1, "text": "UF2 Organizacion de la ejecucion\n\nEl plan general de construccion recoge toda la documentacion."},
    {"index": 2, "text": "El plazo y el cronograma\n\nLa prevencion de riesgos laborales es fundamental."},
]


class PagesTextTest(unittest.TestCase):
    def test_markers_included(self):
        text = outline.build_pages_text(PAGES)
        self.assertIn("[[PAGE_1]]", text)
        self.assertIn("[[PAGE_2]]", text)
        self.assertIn("plan general", text)

    def test_empty_pages_skipped(self):
        text = outline.build_pages_text([{"index": 1, "text": ""}, {"index": 2, "text": "contenido"}])
        self.assertNotIn("[[PAGE_1]]", text)
        self.assertIn("[[PAGE_2]]", text)

    def test_no_text_returns_empty(self):
        self.assertEqual(outline.build_pages_text([]), "")
        self.assertEqual(outline.build_pages_text([{"index": 1, "text": "  "}]), "")


class SplitChunksTest(unittest.TestCase):
    def test_small_text_single_chunk(self):
        text = outline.build_pages_text(PAGES)
        chunks = outline.split_chunks(text)
        self.assertEqual(len(chunks), 1)

    def test_budget_respected_and_markers_kept(self):
        big = [
            {"index": page, "text": "paragraph %d %s" % (page, "x" * 300)} for page in range(1, 30)
        ]
        text = outline.build_pages_text(big)
        chunks = outline.split_chunks(text, budget=2500)
        self.assertGreater(len(chunks), 1)
        for chunk in chunks:
            self.assertLessEqual(len(chunk), 2500 + 400, "允许段落级超出")
            self.assertIn("[[PAGE_", chunk)


class MergeTopicsTest(unittest.TestCase):
    def test_dedupe_by_anchor_and_sort_by_page(self):
        chunk_topics = [
            [
                {"display_title": "总体计划", "search_anchor": "El plan general de construccion", "page_index": 2, "difficulty_score": 30},
                {"display_title": "工期", "search_anchor": "El plazo y el cronograma", "page_index": 5, "difficulty_score": 50},
            ],
            [
                {"display_title": "总体计划（重复）", "search_anchor": "el PLAN GENERAL de construccion", "page_index": 9, "difficulty_score": 30,
                 "subtopics": [{"display_title": "子题", "search_anchor": "nueva evidencia", "page_index": 9}]},
            ],
        ]
        topics = outline.merge_topics(chunk_topics)
        self.assertEqual(len(topics), 2)
        self.assertEqual([t["page_index"] for t in topics], [2, 5])
        # 重复项的子主题并入了先出现的条目
        first = topics[0]
        self.assertEqual(len(first["subtopics"]), 1)

    def test_clean_topic_clamps(self):
        cleaned = outline._clean_topic({
            "display_title": "x" * 200,
            "search_anchor": "a" * 400,
            "page_index": "not-a-number",
            "difficulty_score": 999,
        })
        self.assertEqual(len(cleaned["display_title"]), 60)
        self.assertEqual(len(cleaned["search_anchor"]), 160)
        self.assertEqual(cleaned["page_index"], 0)
        self.assertEqual(cleaned["difficulty_score"], 100)

    def test_invalid_entries_dropped(self):
        topics = outline.merge_topics([[{"display_title": "", "search_anchor": "x"}, "junk", None]])
        self.assertEqual(topics, [])


class EndpointTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        settings = config.Settings(
            data_dir=self._tmp.name,
            llm_base_url="http://127.0.0.1:9/v1",
            llm_api_key="k",
            llm_model="fake",
        )
        self.app = server.create_app(settings)
        self.client = TestClient(self.app)
        reader._OUTLINE_CACHE.clear()  # 大纲缓存在模块级，测试之间必须清

    def tearDown(self):
        self._tmp.cleanup()

    def _post(self, payload):
        return self.client.post("/api/reader/outline", json=payload)

    def test_requires_text(self):
        self.assertEqual(self._post({"pages": [{"index": 1, "text": " "}]}).status_code, 400)
        self.assertEqual(self._post({"pages": []}).status_code, 400)

    def test_returns_topics_and_caches(self):
        reply = (
            '{"topics": [{"display_title": "总体计划", "search_anchor": "El plan general",'
            ' "page_index": 1, "difficulty_score": 35, "subtopics": []}]}'
        )
        with mock.patch("aula.llm.LLMClient.chat", return_value=reply):
            first = self._post({"pages": PAGES}).json()
        self.assertEqual(first["chunks"], 1)
        self.assertEqual(first["topics"][0]["display_title"], "总体计划")
        self.assertFalse(first["cached"])
        with mock.patch("aula.llm.LLMClient.chat", side_effect=AssertionError("不应再调用")):
            second = self._post({"pages": PAGES}).json()
        self.assertTrue(second["cached"])

    def test_llm_failure_is_502(self):
        with mock.patch("aula.llm.LLMClient.chat", side_effect=LLMError("down")):
            response = self._post({"pages": PAGES})
        self.assertEqual(response.status_code, 502)

    def test_all_chunks_bad_json_is_502(self):
        # 全部块都解析失败时必须报错；返回"空大纲"会让用户以为讲义没内容
        with mock.patch("aula.llm.LLMClient.chat", return_value="这不是 JSON"):
            response = self._post({"pages": PAGES})
        self.assertEqual(response.status_code, 502)
        self.assertIn("解析失败", response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
