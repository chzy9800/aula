"""服务商预设与 Gemini 转写的测试。"""

import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aula import config, providers  # noqa: E402
from aula.__main__ import main as cli_main  # noqa: E402
from aula.asr import ASRError, GeminiASR  # noqa: E402


class RegistryTest(unittest.TestCase):
    def test_ids_unique_and_present(self):
        ids = [p.id for p in providers.PROVIDERS]
        self.assertEqual(len(ids), len(set(ids)))
        for expected in ("ollama", "gemini", "groq", "openrouter", "cerebras", "zhipu"):
            self.assertIn(expected, ids)

    def test_every_entry_is_usable(self):
        for provider in providers.PROVIDERS:
            self.assertTrue(provider.base_url.startswith("http"), provider.id)
            self.assertTrue(provider.model, provider.id)
            self.assertTrue(provider.limits, provider.id)
            self.assertIn(provider.kind, ("chat", "asr", "both"), provider.id)
            self.assertIn(provider.asr_mode, ("", "openai", "gemini"), provider.id)
            if provider.asr_mode:
                self.assertTrue(provider.asr_model, "%s 有转写能力但没给模型" % provider.id)

    def test_local_provider_needs_no_key(self):
        ollama = providers.get("ollama")
        self.assertFalse(ollama.key_required)
        self.assertEqual(ollama.needs_key, "不需要（本地）")

    def test_get_is_case_insensitive(self):
        self.assertIs(providers.get("GEMINI"), providers.get("gemini"))
        self.assertIsNone(providers.get("nope"))

    def test_remote_providers_declare_key_env(self):
        for provider in providers.PROVIDERS:
            if provider.local or provider.paid:
                continue
            self.assertTrue(provider.key_env, "%s 应声明密钥环境变量" % provider.id)

    def test_table_contains_all_ids(self):
        table = providers.render_table()
        for provider in providers.PROVIDERS:
            self.assertIn(provider.id, table)
        self.assertIn("免费额度", table)


class ApplyTest(unittest.TestCase):
    def test_preset_fills_chat_and_asr(self):
        settings = config.Settings(provider="groq")
        applied = providers.apply(settings, set())
        self.assertEqual(applied.llm_base_url, "https://api.groq.com/openai/v1")
        self.assertEqual(applied.llm_model, "llama-3.3-70b-versatile")
        self.assertEqual(applied.asr_provider, "openai")
        self.assertEqual(applied.asr_model, "whisper-large-v3-turbo")

    def test_gemini_uses_native_asr(self):
        applied = providers.apply(config.Settings(provider="gemini"), set())
        self.assertEqual(applied.asr_provider, "gemini")
        self.assertIn("generativelanguage", applied.asr_base_url)

    def test_explicit_fields_win(self):
        applied = providers.apply(config.Settings(provider="gemini"), {"llm_model", "llm_base_url"})
        self.assertEqual(applied.llm_model, "qwen3:8b", "显式模型不应被预设覆盖")
        self.assertEqual(applied.llm_base_url, "http://localhost:11434/v1")

    def test_unknown_provider_is_noop(self):
        settings = config.Settings(provider="", llm_model="custom-model")
        applied = providers.apply(settings, set())
        self.assertEqual(applied.llm_model, "custom-model")

    def test_key_read_from_env(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "env-key"}, clear=False):
            applied = providers.apply(config.Settings(provider="gemini"), set())
            self.assertEqual(applied.llm_api_key, "env-key")

    def test_chat_only_provider_keeps_asr(self):
        applied = providers.apply(config.Settings(provider="cerebras", asr_provider="mock"), set())
        self.assertEqual(applied.asr_provider, "mock", "只做对话的预设不该动转写配置")

    def test_wbmanager_preset_points_at_local_gateway(self):
        applied = providers.apply(config.Settings(provider="wbmanager"), set())
        self.assertEqual(applied.llm_base_url, "http://127.0.0.1:7864/v1")
        self.assertEqual(applied.llm_model, "cn:glm-5.3-flash")
        self.assertEqual(applied.asr_provider, "auto", "网关只做对话，别动转写")

    def test_reasoning_effort_follows_preset_and_clears(self):
        applied = providers.apply(config.Settings(provider="wbmanager"), set())
        self.assertEqual(applied.llm_reasoning_effort, "low")
        # 切回不认这个参数的预设时必须清掉，否则请求直接 400
        plain = providers.apply(
            config.Settings(provider="gemini", llm_reasoning_effort="low"), set()
        )
        self.assertEqual(plain.llm_reasoning_effort, "")

    def test_public_config_exposes_providers(self):
        public = config.Settings(provider="gemini").public()
        self.assertEqual(len(public["providers"]), len(providers.PROVIDERS))
        self.assertIn("limits", public["providers"][0])
        self.assertNotIn("sk-", json.dumps(public["providers"]))


class GeminiASRTest(unittest.TestCase):
    def fake_response(self, text="Hola a todos."):
        payload = {"candidates": [{"content": {"parts": [{"text": text}]}}]}

        class FakeHandle(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class FakeOpener:
            def open(self, request, timeout=None):
                FakeOpener.last_request = request
                return FakeHandle(json.dumps(payload).encode("utf-8"))

        return FakeOpener

    def test_parses_text(self):
        with mock.patch("aula.asr.opener_for", return_value=self.fake_response()()):
            asr = GeminiASR(base_url="", api_key="k")
            result = asr._transcribe_sync(b"\x00\x00" * 1600, 16000, "es")
            self.assertEqual(result.text, "Hola a todos.")
            self.assertEqual(result.provider, "gemini")

    def test_missing_key_raises(self):
        asr = GeminiASR(base_url="", api_key="")
        with self.assertRaises(ASRError) as ctx:
            asr._transcribe_sync(b"\x00\x00" * 100, 16000, "es")
        self.assertIn("密钥", str(ctx.exception))

    def test_blocked_prompt_raises(self):
        payload = {"promptFeedback": {"blockReason": "SAFETY"}}

        class Handle(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        class Opener:
            def open(self, request, timeout=None):
                return Handle(json.dumps(payload).encode())

        with mock.patch("aula.asr.opener_for", return_value=Opener()):
            with self.assertRaises(ASRError) as ctx:
                GeminiASR(base_url="", api_key="k")._transcribe_sync(b"\x00\x00" * 800, 16000, "es")
        self.assertIn("拒绝", str(ctx.exception))

    def test_request_shape(self):
        opener = self.fake_response()()
        with mock.patch("aula.asr.opener_for", return_value=opener):
            GeminiASR(base_url="", api_key="secret")._transcribe_sync(b"\x01\x00" * 800, 16000, "es")
        request = type(opener).last_request
        self.assertIn("generateContent", request.full_url)
        self.assertIn("key=secret", request.full_url)
        body = json.loads(request.data.decode("utf-8"))
        parts = body["contents"][0]["parts"]
        self.assertEqual(parts[1]["inline_data"]["mime_type"], "audio/wav")
        self.assertTrue(parts[1]["inline_data"]["data"])


class CliTest(unittest.TestCase):
    def run_cli(self, argv):
        out = io.StringIO()
        backup = sys.stdout
        sys.stdout = out
        try:
            code = cli_main(argv)
        finally:
            sys.stdout = backup
        return code, out.getvalue()

    def test_providers_table(self):
        code, output = self.run_cli(["providers"])
        self.assertEqual(code, 0)
        self.assertIn("gemini", output)
        self.assertIn("免费额度", output)

    def test_providers_detail(self):
        code, output = self.run_cli(["providers", "--id", "gemini"])
        self.assertEqual(code, 0)
        self.assertIn("generativelanguage", output)
        self.assertIn("GEMINI_API_KEY", output)

    def test_providers_unknown_id(self):
        code, output = self.run_cli(["providers", "--id", "nope"])
        self.assertEqual(code, 1)
        self.assertIn("没有这个预设", output)

    def test_providers_json(self):
        code, output = self.run_cli(["providers", "--json"])
        self.assertEqual(code, 0)
        data = json.loads(output)
        self.assertTrue(any(item["id"] == "groq" for item in data))


if __name__ == "__main__":
    unittest.main()
