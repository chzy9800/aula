"""接口与实时链路测试。用假 LLM，不打真实模型。"""

import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from aula import config  # noqa: E402
from aula.server import create_app  # noqa: E402

SUMMARY_PAYLOAD = {
    "overview": "本节课讲总体施工计划。",
    "points": [{"heading": "计划的作用", "detail": "汇总全部技术文件。"}],
    "terms": [{"term": "plan general", "translation": "总体计划", "note": "施工总纲"}],
    "todos": ["阅读下一节"],
    "questions": ["工期怎么拆？"],
    "emphasis": ["工期是重点"],
}


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.translate_calls = 0
        self.summary_calls = 0

    def chat(self, messages, **kwargs):
        self.translate_calls += 1
        return "中文译文"

    def chat_json(self, messages, **kwargs):
        self.summary_calls += 1
        return dict(SUMMARY_PAYLOAD)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # 隔离配置文件：PUT /api/config 会写盘，不能碰用户真实的 ~/.config
        self._env_backup = os.environ.get("AULA_CONFIG")
        os.environ["AULA_CONFIG"] = str(Path(self._tmp.name) / "config.json")
        self.settings = config.Settings(
            data_dir=self._tmp.name,
            asr_provider="mock",
            source_lang="es",
            target_lang="zh",
            llm_model="fake",
            auto_summary_minutes=0,
        )
        self.app = create_app(self.settings)
        self.fake = FakeLLM()
        self.app.state.engines.client = self.fake
        self.app.state.engines.translator.client = self.fake
        self.client = TestClient(self.app)

    def tearDown(self):
        if self._env_backup is None:
            os.environ.pop("AULA_CONFIG", None)
        else:
            os.environ["AULA_CONFIG"] = self._env_backup
        self._tmp.cleanup()

    def new_session(self, title="测试课"):
        response = self.client.post("/api/sessions", json={"title": title, "course": "MF1180"})
        self.assertEqual(response.status_code, 200)
        return response.json()["id"]

    # —— 基础 ——

    def test_health(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["asr"]["provider"], "mock")

    def test_index_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("aula", response.text)

    def test_config_roundtrip(self):
        body = self.client.get("/api/config").json()
        self.assertEqual(body["source_lang"], "es")
        self.assertIn("languages", body)
        self.assertNotIn("sk-", str(body.get("llm_api_key", "")))

        updated = self.client.put("/api/config", json={"target_lang": "en", "silence_ms": 900}).json()
        self.assertEqual(updated["target_lang"], "en")
        self.assertEqual(updated["silence_ms"], 900)

    # —— 会话 ——

    def test_session_crud(self):
        sid = self.new_session()
        listing = self.client.get("/api/sessions").json()
        self.assertEqual(len(listing), 1)

        detail = self.client.get(f"/api/sessions/{sid}").json()
        self.assertEqual(detail["session"]["title"], "测试课")
        self.assertEqual(detail["utterances"], [])

        patched = self.client.patch(f"/api/sessions/{sid}", json={"title": "改名"}).json()
        self.assertEqual(patched["title"], "改名")

        self.assertEqual(self.client.delete(f"/api/sessions/{sid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/sessions/{sid}").status_code, 404)

    def test_note_save_and_load(self):
        sid = self.new_session()
        self.client.put(f"/api/sessions/{sid}/note", json={"content": "# 笔记\n内容"})
        detail = self.client.get(f"/api/sessions/{sid}").json()
        self.assertIn("# 笔记", detail["note"])

    def test_manual_utterance(self):
        sid = self.new_session()
        response = self.client.post(
            f"/api/sessions/{sid}/utterance",
            json={"source": "Hola", "target": "你好", "start_ms": 1000, "end_ms": 2000},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["seq"], 1)

    # —— 总结与导出 ——

    def test_summary_flow(self):
        sid = self.new_session()
        self.client.post(f"/api/sessions/{sid}/utterance", json={"source": "Hola", "target": "你好"})

        empty = self.client.post(f"/api/sessions/{sid}/summary", json={})
        self.assertEqual(empty.status_code, 200)
        body = empty.json()
        self.assertEqual(body["content"]["overview"], "本节课讲总体施工计划。")
        self.assertEqual(body["covers_upto"], 1)

        latest = self.client.get(f"/api/sessions/{sid}/summary").json()
        self.assertEqual(latest["model"], "fake")

    def test_summary_requires_content(self):
        sid = self.new_session()
        response = self.client.post(f"/api/sessions/{sid}/summary", json={})
        self.assertEqual(response.status_code, 400)

    def test_summary_missing_session(self):
        self.assertEqual(self.client.post("/api/sessions/nope/summary", json={}).status_code, 404)

    def test_export_markdown(self):
        sid = self.new_session()
        self.client.post(f"/api/sessions/{sid}/utterance", json={"source": "Hola", "target": "你好"})
        self.client.put(f"/api/sessions/{sid}/note", json={"content": "我的笔记"})
        self.client.post(f"/api/sessions/{sid}/summary", json={})

        response = self.client.get(f"/api/sessions/{sid}/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/markdown", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("## 课堂总结", response.text)
        self.assertIn("我的笔记", response.text)

    # —— 实时链路 ——

    def test_websocket_transcribes_and_translates(self):
        sid = self.new_session()
        pcm = b"\x00\x00" * 16000  # 1 秒静音，MockASR 不关心内容
        header = struct.pack("<II", 0, 1000)

        with self.client.websocket_connect(f"/ws/live?session_id={sid}") as ws:
            ready = ws.receive_json()
            self.assertEqual(ready["type"], "ready")
            self.assertEqual(ready["asr"]["provider"], "mock")

            ws.send_bytes(header + pcm)
            message = ws.receive_json()
            self.assertEqual(message["type"], "transcript")
            utterance = message["utterance"]
            self.assertTrue(utterance["source"])
            self.assertEqual(utterance["target"], "中文译文")
            self.assertEqual(utterance["seq"], 1)

        detail = self.client.get(f"/api/sessions/{sid}").json()
        self.assertEqual(len(detail["utterances"]), 1)

    def test_websocket_rejects_unknown_session(self):
        with self.client.websocket_connect("/ws/live?session_id=nope") as ws:
            message = ws.receive_json()
            self.assertEqual(message["type"], "error")

    def test_websocket_ping(self):
        sid = self.new_session()
        with self.client.websocket_connect(f"/ws/live?session_id={sid}") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({"type": "ping"}))
            self.assertEqual(ws.receive_json()["type"], "pong")


if __name__ == "__main__":
    unittest.main()
