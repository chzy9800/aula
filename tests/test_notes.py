"""归纳与规范化测试。用假客户端，不打真实接口。"""

import sys
import unittest
from pathlib import Path



from aula.llm import LLMError, extract_json  # noqa: E402
from aula.digest import build_notes, normalize  # noqa: E402
from aula.transcript import BilingualTurn  # noqa: E402


class FakeClient:
    """按顺序吐出预设响应，并记录收到的请求。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.model = "fake-model"

    def chat_json(self, messages, **kwargs):
        self.calls.append(messages)
        if not self.responses:
            raise LLMError("没有更多预设响应")
        return self.responses.pop(0)


class ExtractJsonTest(unittest.TestCase):
    def test_plain_json(self):
        self.assertEqual(extract_json('{"a": 1}'), {"a": 1})

    def test_fenced_json(self):
        self.assertEqual(extract_json('说明文字\n```json\n{"a": 2}\n```\n尾巴'), {"a": 2})

    def test_dirty_text_around_object(self):
        self.assertEqual(extract_json('好的，结果如下：{"a": 3} 希望有用'), {"a": 3})

    def test_braces_inside_string(self):
        parsed = extract_json('{"note": "含 } 括号"}')
        self.assertEqual(parsed["note"], "含 } 括号")

    def test_invalid_returns_none(self):
        self.assertIsNone(extract_json("完全不是 JSON"))

    def test_thinking_chain_before_answer(self):
        # 推理模型偶发把推敲写进正文（ pseudo-JSON 不是合法 JSON，扫不过）
        text = ('我先想一下输出格式：{"terms": [...]} 这样不对，改成正文明例。\n'
                '最终答案：{"terms": [{"term": "cronograma", "zh": "进度表"}]}')
        parsed = extract_json(text)
        self.assertEqual(parsed["terms"][0]["term"], "cronograma")

    def test_truncated_object_is_repaired(self):
        parsed = extract_json('{"points": ["第一条", "第二条"], "terms": [{"term": "plaz')
        self.assertEqual(parsed["points"], ["第一条", "第二条"])
        self.assertEqual(parsed["terms"][0]["term"], "plaz")

    def test_unrepairable_truncation_returns_none(self):
        # 正好切断在键名中间：补不回来，认输（上层会重试）
        self.assertIsNone(extract_json('{"points": ["甲", "乙"], "na'))


class NormalizeTest(unittest.TestCase):
    def test_points_as_strings(self):
        notes = normalize({"title": "施工计划", "points": ["要点一", "要点二"]})
        self.assertEqual(len(notes.points), 2)
        self.assertEqual(notes.points[0].detail, "要点一")

    def test_points_as_objects(self):
        notes = normalize({"points": [{"heading": "标题", "detail": "细节"}]})
        self.assertEqual(notes.points[0].heading, "标题")
        self.assertEqual(notes.points[0].detail, "细节")

    def test_terms_deduplicated(self):
        notes = normalize({"terms": [{"term": "plan"}, {"term": "Plan"}, {"term": "obra"}]})
        self.assertEqual(len(notes.terms), 2)

    def test_missing_fields_safe(self):
        notes = normalize({})
        self.assertEqual(notes.title, "未命名课堂")
        self.assertEqual(notes.emphasis, [])

    def test_emphasis_as_string(self):
        notes = normalize({"emphasis": "这是考点"})
        self.assertEqual(notes.emphasis, ["这是考点"])


class BuildNotesTest(unittest.TestCase):
    def turns(self):
        return [
            BilingualTurn("Vamos a ver el plan general de construcción.", "我们来看总体施工计划。"),
            BilingualTurn("El plazo es de cinco semanas.", "工期是五周。"),
        ]

    def test_two_stage_pipeline(self):
        client = FakeClient(
            [
                {"points": ["总体施工计划是第一阶段的产物"], "terms": [{"term": "plan general"}], "emphasis": [], "assignments": [], "questions": []},
                {
                    "title": "电气装置施工总体计划",
                    "summary": "讲解总体施工计划的构成。",
                    "points": [{"heading": "总体计划", "detail": "所有技术文件的集合。"}],
                    "terms": [{"term": "plan general", "translation": "总体计划", "note": "施工总纲"}],
                    "emphasis": ["考点：工期"],
                    "assignments": [],
                    "questions": ["五周如何拆分？"],
                },
            ]
        )
        notes = build_notes(self.turns(), client, chunk_budget=10000)
        self.assertEqual(notes.title, "电气装置施工总体计划")
        self.assertEqual(len(notes.points), 1)
        self.assertEqual(notes.terms[0].translation, "总体计划")
        self.assertEqual(notes.chunks, 1)
        self.assertGreater(notes.source_chars, 0)
        # 第一次是分块抽取，第二次是全局合成
        self.assertEqual(len(client.calls), 2)

    def test_all_chunks_failed_raises(self):
        client = FakeClient([{"points": []}])
        with self.assertRaises(LLMError):
            build_notes(self.turns(), client, chunk_budget=10000)


if __name__ == "__main__":
    unittest.main()
