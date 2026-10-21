"""专用翻译接口（Google / Azure / DeepL）测试。全部走假 HTTP。"""

import io
import json
import os
import sys
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aula import config, mt  # noqa: E402
from aula.translate import Translator  # noqa: E402


class FakeOpener:
    """记录请求，返回预置 JSON。"""

    def __init__(self, payload, status: int = 200):
        self.payload = payload
        self.status = status
        self.request = None

    def open(self, request, timeout=None):
        self.request = request
        if self.status != 200:
            import urllib.error

            raise urllib.error.HTTPError(
                request.full_url, self.status, "err", {}, io.BytesIO(json.dumps(self.payload).encode())
            )
        handle = io.BytesIO(json.dumps(self.payload, ensure_ascii=False).encode("utf-8"))
        handle.__enter__ = lambda: handle  # type: ignore
        handle.__exit__ = lambda *args: False  # type: ignore
        return handle


class BuildTest(unittest.TestCase):
    def test_llm_means_no_mt(self):
        self.assertIsNone(mt.build("llm", "k"))
        self.assertIsNone(mt.build("", "k"))

    def test_unknown_engine(self):
        with self.assertRaises(mt.MTError):
            mt.build("baidu", "k")

    def test_missing_key(self):
        for engine in ("google", "azure", "deepl"):
            with self.assertRaises(mt.MTError):
                mt.build(engine, "")

    def test_instances(self):
        self.assertIsInstance(mt.build("google", "k"), mt.GoogleTranslateMT)
        self.assertIsInstance(mt.build("azure", "k", "westeurope"), mt.AzureTranslatorMT)
        self.assertIsInstance(mt.build("deepl", "k"), mt.DeepLMT)


class GoogleTest(unittest.TestCase):
    def test_request_and_unescape(self):
        opener = FakeOpener({"data": {"translations": [{"translatedText": "你好 &amp; 世界"}]}})
        with mock.patch("aula.mt.opener_for", return_value=opener):
            text = mt.GoogleTranslateMT("key-123").translate("Hola & mundo", "es", "zh")
        self.assertEqual(text, "你好 & 世界")
        self.assertIn("key=key-123", opener.request.full_url)
        body = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(body["target"], "zh-CN", "中文应映射成 zh-CN")
        self.assertEqual(body["source"], "es")
        self.assertEqual(body["format"], "text")


class AzureTest(unittest.TestCase):
    def test_headers_and_query(self):
        opener = FakeOpener([{"translations": [{"text": "总是应该从总体施工计划开始"}]}])
        with mock.patch("aula.mt.opener_for", return_value=opener):
            text = mt.AzureTranslatorMT("azure-key", "westeurope").translate("Siempre...", "es", "zh")
        self.assertEqual(text, "总是应该从总体施工计划开始")
        url = opener.request.full_url
        self.assertIn("api-version=3.0", url)
        self.assertIn("to=zh-Hans", url)
        self.assertEqual(opener.request.headers["Ocp-apim-subscription-key"], "azure-key")
        self.assertEqual(opener.request.headers["Ocp-apim-subscription-region"], "westeurope")
        self.assertEqual(json.loads(opener.request.data.decode("utf-8"))[0]["Text"], "Siempre...")

    def test_region_optional(self):
        opener = FakeOpener([{"translations": [{"text": "x"}]}])
        with mock.patch("aula.mt.opener_for", return_value=opener):
            mt.AzureTranslatorMT("k").translate("a", "es", "zh")
        self.assertNotIn("Ocp-apim-subscription-region", opener.request.headers)


class DeepLTest(unittest.TestCase):
    def test_form_body(self):
        opener = FakeOpener({"translations": [{"text": "工期是五周"}]})
        with mock.patch("aula.mt.opener_for", return_value=opener):
            text = mt.DeepLMT("deepl-key").translate("El plazo es de cinco semanas.", "es", "zh")
        self.assertEqual(text, "工期是五周")
        self.assertIn("DeepL-Auth-Key", opener.request.headers["Authorization"])
        form = urllib.parse.parse_qs(opener.request.data.decode("utf-8"))
        self.assertEqual(form["target_lang"], ["ZH-HANS"])
        self.assertEqual(form["source_lang"], ["ES"])


class ErrorTest(unittest.TestCase):
    def test_quota_error_has_hint(self):
        opener = FakeOpener({"error": "quota"}, status=456)
        with mock.patch("aula.mt.opener_for", return_value=opener):
            with self.assertRaises(mt.MTError) as ctx:
                mt.DeepLMT("k").translate("a", "es", "zh")
        self.assertIn("额度", str(ctx.exception))

    def test_rate_limit_error(self):
        opener = FakeOpener({"error": "slow down"}, status=429)
        with mock.patch("aula.mt.opener_for", return_value=opener):
            with self.assertRaises(mt.MTError) as ctx:
                mt.GoogleTranslateMT("k").translate("a", "es", "zh")
        self.assertIn("429", str(ctx.exception))


class TranslatorFallbackTest(unittest.TestCase):
    class FakeLLM:
        def __init__(self):
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return "大模型译文"

    class BrokenMT(mt.BaseMT):
        name = "broken"

        def translate(self, text, source, target):
            raise mt.MTError("接口挂了")

    class WorkingMT(mt.BaseMT):
        name = "working"

        def translate(self, text, source, target):
            return "专用接口译文"

    def test_uses_mt_when_available(self):
        translator = Translator(self.FakeLLM(), "es", "zh", mt=self.WorkingMT())
        self.assertEqual(translator.translate("Hola"), "专用接口译文")

    def test_mt_cached(self):
        working = mock.Mock(wraps=self.WorkingMT())
        translator = Translator(self.FakeLLM(), "es", "zh", mt=working)
        translator.translate("Hola")
        translator.translate("Hola")
        self.assertEqual(working.translate.call_count, 1)

    def test_falls_back_to_llm(self):
        llm = self.FakeLLM()
        translator = Translator(llm, "es", "zh", mt=self.BrokenMT())
        self.assertEqual(translator.translate("Hola"), "大模型译文")
        self.assertEqual(llm.calls, 1)

    def test_without_mt_uses_llm(self):
        llm = self.FakeLLM()
        translator = Translator(llm, "es", "zh")
        self.assertEqual(translator.translate("Hola"), "大模型译文")


class ConfigTest(unittest.TestCase):
    def test_pick_mt(self):
        self.assertEqual(config.Settings(translate_engine="llm").pick_mt(), "llm")
        self.assertEqual(config.Settings(translate_engine="").pick_mt(), "llm")
        self.assertEqual(config.Settings(translate_engine="AZURE").pick_mt(), "azure")

    def test_env_override(self):
        with mock.patch.dict(os.environ, {"AULA_TRANSLATE_ENGINE": "deepl", "AULA_MT_API_KEY": "k"}, clear=False):
            settings = config.load({})
        self.assertEqual(settings.pick_mt(), "deepl")
        self.assertEqual(settings.mt_api_key, "k")

    def test_public_hides_key(self):
        public = config.Settings(translate_engine="azure", mt_api_key="secret-value-1234").public()
        self.assertNotEqual(public["mt_api_key"], "secret-value-1234")
        self.assertIn("*", public["mt_api_key"])
        self.assertEqual(len(public["mt_providers"]), len(mt.MT_PROVIDERS))


class TableTest(unittest.TestCase):
    def test_table_lists_all(self):
        table = mt.render_table()
        for provider in mt.MT_PROVIDERS:
            self.assertIn(provider.id, table)
        self.assertIn("额度 / 限制", table)
        self.assertIn("免密钥", table)


if __name__ == "__main__":
    unittest.main()


class KeylessBuildTest(unittest.TestCase):
    def test_build_without_key(self):
        for engine in ("google_free", "mymemory", "libre"):
            client = mt.build(engine, "")
            self.assertIsNotNone(client, engine)
            self.assertIn("免密钥", client.describe().get("key", ""))

    def test_registry_marks_keyless(self):
        for engine in ("google_free", "mymemory", "libre"):
            provider = mt.get(engine)
            self.assertFalse(provider.key_required, engine)
            self.assertEqual(provider.needs_key(), "不需要（免密钥）")


class GoogleFreeTest(unittest.TestCase):
    def test_parses_flat_array(self):
        opener = FakeOpener(["截止日期是五周。"])
        with mock.patch("aula.mt.opener_for", return_value=opener):
            text = mt.GoogleFreeMT().translate("El plazo es de cinco semanas.", "es", "zh")
        self.assertEqual(text, "截止日期是五周。")
        url = opener.request.full_url
        self.assertIn("client=dict-chrome-ex", url)
        self.assertIn("tl=zh-CN", url)
        self.assertIn("Mozilla", opener.request.headers["User-agent"])

    def test_parses_nested_array(self):
        nested = [[["职业风险的预防至关重要", "La prevención...", None, None]], None, "es"]
        opener = FakeOpener(nested)
        with mock.patch("aula.mt.opener_for", return_value=opener):
            text = mt.GoogleFreeMT().translate("La prevención...", "es", "zh")
        self.assertEqual(text, "职业风险的预防至关重要")

    def test_blocked_page_falls_back_then_raises(self):
        opener = FakeOpener({"x": 1})
        opener.payload = "<html>Sorry...</html>"
        with mock.patch("aula.mt.opener_for", return_value=opener):
            with self.assertRaises(mt.MTError) as ctx:
                mt.GoogleFreeMT().translate("Hola.", "es", "zh")
        self.assertIn("限流", str(ctx.exception))


class MyMemoryTest(unittest.TestCase):
    def test_parses_translation(self):
        payload = {"responseData": {"translatedText": "截止日期为五周。"}, "responseStatus": 200}
        opener = FakeOpener(payload)
        with mock.patch("aula.mt.opener_for", return_value=opener):
            text = mt.MyMemoryMT().translate("El plazo es de cinco semanas.", "es", "zh")
        self.assertEqual(text, "截止日期为五周。")
        self.assertIn("langpair=es%7Czh-CN", opener.request.full_url)

    def test_quota_finished(self):
        opener = FakeOpener({"responseData": {"translatedText": ""}, "quotaFinished": True})
        with mock.patch("aula.mt.opener_for", return_value=opener):
            with self.assertRaises(mt.MTError) as ctx:
                mt.MyMemoryMT().translate("Hola", "es", "zh")
        self.assertIn("额度", str(ctx.exception))

    def test_bad_status(self):
        opener = FakeOpener({"responseStatus": 403, "responseDetails": "INVALID LANGUAGE PAIR"})
        with mock.patch("aula.mt.opener_for", return_value=opener):
            with self.assertRaises(mt.MTError):
                mt.MyMemoryMT().translate("Hola", "es", "zh")


class LibreTest(unittest.TestCase):
    def test_posts_json(self):
        opener = FakeOpener({"translatedText": "期限为5周."})
        with mock.patch("aula.mt.opener_for", return_value=opener):
            text = mt.LibreTranslateMT().translate("El plazo es de cinco semanas.", "es", "zh")
        self.assertEqual(text, "期限为5周.")
        body = json.loads(opener.request.data.decode("utf-8"))
        self.assertEqual(body["target"], "zh", "LibreTranslate 用 zh 而不是 zh-CN")
        self.assertEqual(body["format"], "text")

    def test_missing_field(self):
        opener = FakeOpener({"error": "slow down"})
        with mock.patch("aula.mt.opener_for", return_value=opener):
            with self.assertRaises(mt.MTError):
                mt.LibreTranslateMT().translate("Hola", "es", "zh")
