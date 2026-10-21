"""存储层测试：会话、转写、笔记、总结、导出。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aula import store  # noqa: E402


class StoreTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db = Path(self._tmp.name) / "test.db"
        store.init_db(self.db)

    def tearDown(self):
        self._tmp.cleanup()

    def test_create_and_list(self):
        session = store.create_session(self.db, title="测试课", course="MF1180")
        self.assertEqual(session["title"], "测试课")
        self.assertEqual(session["utterance_count"], 0)
        sessions = store.list_sessions(self.db)
        self.assertEqual(len(sessions), 1)
        self.assertEqual(sessions[0]["id"], session["id"])

    def test_default_title_when_empty(self):
        session = store.create_session(self.db)
        self.assertTrue(session["title"])

    def test_utterances_are_ordered(self):
        session = store.create_session(self.db)
        sid = session["id"]
        for index in range(3):
            store.add_utterance(self.db, sid, source="s%d" % index, target="t%d" % index, start_ms=index * 1000)
        rows = store.list_utterances(self.db, sid)
        self.assertEqual([r["seq"] for r in rows], [1, 2, 3])
        self.assertEqual([r["source"] for r in rows], ["s0", "s1", "s2"])

    def test_note_upsert(self):
        sid = store.create_session(self.db)["id"]
        store.save_note(self.db, sid, "第一版")
        store.save_note(self.db, sid, "第二版")
        self.assertEqual(store.get_note(self.db, sid), "第二版")

    def test_summary_roundtrip(self):
        sid = store.create_session(self.db)["id"]
        content = {"overview": "讲了施工计划", "points": [{"heading": "计划", "detail": "细节"}], "terms": []}
        record = store.add_summary(self.db, sid, content, model="fake", covers_upto=5)
        self.assertEqual(record["content"]["overview"], "讲了施工计划")
        latest = store.latest_summary(self.db, sid)
        self.assertEqual(latest["covers_upto"], 5)

    def test_cascade_delete(self):
        sid = store.create_session(self.db)["id"]
        store.add_utterance(self.db, sid, source="x")
        store.save_note(self.db, sid, "笔记")
        self.assertTrue(store.delete_session(self.db, sid))
        self.assertEqual(store.list_utterances(self.db, sid), [])
        self.assertIsNone(store.get_session(self.db, sid))

    def test_export_markdown(self):
        sid = store.create_session(self.db, title="电气课")["id"]
        store.add_utterance(self.db, sid, source="Hola a todos", target="大家好", start_ms=1000)
        store.save_note(self.db, sid, "## 我的笔记\n- 重点")
        store.add_summary(self.db, sid, {"overview": "总览", "points": [{"heading": "A", "detail": "B"}]}, model="m")
        markdown = store.export_markdown(self.db, sid)
        self.assertIn('title: "电气课"', markdown)
        self.assertIn("## 课堂总结", markdown)
        self.assertIn("## 课堂笔记", markdown)
        self.assertIn("## 转写全文", markdown)
        self.assertIn("Hola a todos", markdown)
        self.assertIn("大家好", markdown)

    def test_export_without_transcript(self):
        sid = store.create_session(self.db, title="无转写")["id"]
        markdown = store.export_markdown(self.db, sid, include_transcript=False)
        self.assertNotIn("## 转写全文", markdown)

    def test_export_missing_session(self):
        with self.assertRaises(KeyError):
            store.export_markdown(self.db, "nope")


if __name__ == "__main__":
    unittest.main()
