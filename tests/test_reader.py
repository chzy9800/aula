"""讲义工作区接口测试：翻译降级链、圈选提问。

全部走假实现，不触网：翻译用假引擎，提问用假模型。重点验证「一条链路挂了
不能把整页译文带走」——这是上一版真实踩过的坑。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from aula import config, llm as llmmod, reader, server  # noqa: E402
from aula.mt import MTError  # noqa: E402


class FakeMT:
    """假翻译接口：返回可预测的译文，可选抛错。"""

    def __init__(self, prefix: str = "译:", fail: bool = False) -> None:
        self.prefix = prefix
        self.fail = fail

    def translate(self, text: str, source: str, target: str) -> str:
        if self.fail:
            raise MTError("被限流")
        return self.prefix + text[:20]


class ReaderTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.settings = config.Settings(
            data_dir=self._tmp.name,
            translate_engine="google_free",
            llm_base_url="http://127.0.0.1:9/v1",
            llm_api_key="k",
            llm_model="fake",
        )
        self.app = server.create_app(self.settings)
        self.client = TestClient(self.app)

    def tearDown(self):
        self._tmp.cleanup()

    def translate(self, blocks, **extra):
        payload = {"blocks": blocks, "source": "es", "target": "zh", **extra}
        return self.client.post("/api/reader/translate", json=payload)


class TranslateTest(ReaderTestBase):
    def test_mt_engine_returns_translations(self):
        with mock.patch.object(reader.mtmod, "build", return_value=FakeMT()):
            response = self.translate(["Hola a todos.", "El plazo es de cinco semanas."])
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["engine"], "google_free")
        self.assertFalse(body["degraded"])
        self.assertEqual(len(body["translations"]), 2)
        self.assertTrue(body["translations"][0].startswith("译:"))

    def test_empty_blocks(self):
        body = self.translate([]).json()
        self.assertEqual(body["translations"], [])

    def test_mt_failure_falls_back_to_llm(self):
        with mock.patch.object(reader.mtmod, "build", return_value=FakeMT(fail=True)), \
             mock.patch.object(llmmod.LLMClient, "chat", return_value="1. 第一段\n2. 第二段"):
            body = self.translate(["uno", "dos"]).json()
        self.assertEqual(body["engine"], "llm")
        self.assertTrue(body["degraded"])
        self.assertEqual(body["translations"], ["第一段", "第二段"])

    def test_all_engines_fail_returns_empty_not_error(self):
        with mock.patch.object(reader.mtmod, "build", return_value=FakeMT(fail=True)), \
             mock.patch.object(llmmod.LLMClient, "chat", side_effect=reader.LLMError("down")):
            body = self.translate(["uno"]).json()
        self.assertEqual(body["translations"], [""])
        self.assertTrue(body["degraded"])

    def test_llm_numbering_stripped_and_padded(self):
        with mock.patch.object(llmmod.LLMClient, "chat", return_value="1. 甲"):
            body = self.translate(["uno", "dos"], engine="llm").json()
        self.assertEqual(body["engine"], "llm")
        self.assertEqual(len(body["translations"]), 2)
        self.assertEqual(body["translations"], ["甲", ""])


class AskTest(ReaderTestBase):
    def test_rejects_empty_question(self):
        self.assertEqual(self.client.post("/api/reader/ask", json={"question": "  "}).status_code, 400)

    def test_prompt_carries_selection_and_page(self):
        captured = {}

        def fake_chat(client_self, messages, *args, **kwargs):
            captured["messages"] = messages
            return "这里是解释。"

        with mock.patch.object(llmmod.LLMClient, "chat", autospec=True, side_effect=fake_chat):
            body = self.client.post(
                "/api/reader/ask",
                json={"question": "为什么？", "selection_text": "圈中的句子", "page_text": "整页内容"},
            ).json()
        self.assertEqual(body["answer"], "这里是解释。")
        user = captured["messages"][-1]["content"]
        text = user if isinstance(user, str) else " ".join(part.get("text", "") for part in user)
        self.assertIn("圈中的句子", text)
        self.assertIn("整页内容", text)
        self.assertIn("为什么？", text)

    def test_model_failure_returns_503(self):
        with mock.patch.object(llmmod.LLMClient, "chat", side_effect=reader.LLMError("连接失败")):
            response = self.client.post("/api/reader/ask", json={"question": "这是什么？"})
        self.assertEqual(response.status_code, 503)
        self.assertIn("模型", response.json()["detail"])


class StaticTest(ReaderTestBase):
    def test_health_reports_reader_and_digest(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["reader"]["engine"], "google_free")
        self.assertIn("model", body["digest"])

    def test_both_workspaces_serve(self):
        live = self.client.get("/")
        reader_page = self.client.get("/reader")
        self.assertIn("aula", live.text)
        self.assertIn("aula", reader_page.text)
        self.assertIn("/static/reader.js", reader_page.text)

    def test_sample_pdf(self):
        sample = self.client.get("/api/sample")
        self.assertEqual(sample.status_code, 200)
        self.assertTrue(sample.content.startswith(b"%PDF"))


if __name__ == "__main__":
    unittest.main()
