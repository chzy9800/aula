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


class FragmentContextTest(unittest.TestCase):
    """残句要带上一句：静音落在句中时，"de nuestro diferencial." 单独译会崩。"""

    class RecordingLLM:
        def __init__(self):
            self.users = []
            self.systems = []

        def chat(self, messages, **kwargs):
            self.users.append(messages[-1]["content"])
            self.systems.append(messages[0]["content"])
            return "译文"

    def test_continuation_detection(self):
        from aula.translate import _is_continuation

        # 实测里被切断的残句
        for text in ("de nuestro diferencial.", "por tradición eléctrica.",
                     "como protocolo seguir siempre", "entramos siempre por",
                     "y el neutro", "de tensiones en las protecciones"):
            self.assertTrue(_is_continuation(text, "es"), text)
        # 完整句子不该触发
        for text in ("El diferencial protege a las personas.",
                     "Hay que medir la tensión de alimentación antes de intervenir.",
                     "Ahora bien."):
            self.assertFalse(_is_continuation(text, "es"), text)

    def test_fragment_gets_previous_sentence(self):
        llm = self.RecordingLLM()
        translator = Translator(llm, "es", "zh")
        translator.translate("Otra cosa sencilla que hay que comprobar siempre.")
        translator.translate("de nuestro diferencial.")
        self.assertNotIn("上一句", llm.users[0], "完整句子不该带上下文（守住逐句低延迟）")
        self.assertIn("上一句", llm.users[1])
        self.assertIn("Otra cosa sencilla", llm.users[1])

    def test_english_continuations(self):
        from aula.translate import _is_continuation

        self.assertTrue(_is_continuation("and the neutral wire", "en"))
        self.assertTrue(_is_continuation("which means the circuit", "en"))
        self.assertFalse(_is_continuation("The breaker trips at 25 amperes.", "en"))

    def test_first_sentence_has_no_context(self):
        llm = self.RecordingLLM()
        Translator(llm, "es", "zh").translate("de nuestro diferencial.")
        self.assertNotIn("上一句", llm.users[0], "没有上一句可带")


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


class TermHarvestTest(unittest.TestCase):
    """从译文里回收术语对：同一术语在一节课里必须只有一种译法。"""

    def harvest(self, *samples):
        from collections import OrderedDict

        from aula.translate import harvest_terms

        glossary = OrderedDict()
        for sample in samples:
            harvest_terms(sample, glossary)
        return glossary

    def test_learns_pairs_from_real_translations(self):
        glossary = self.harvest(
            "挂牌上锁（consignación）确保电线路上不带电压。",
            "进度表/时间表（cronograma）决定每项活动的日期。",
            "断路器（interruptor）跳闸了。",
        )
        self.assertEqual(glossary["consignación"], "挂牌上锁")
        self.assertEqual(glossary["cronograma"], "进度表", "带别名时取第一个")
        self.assertEqual(glossary["interruptor"], "断路器")

    def test_skips_explanatory_parentheses(self):
        # 模型写成「价差/差额（diferencial：价差/差额）」是在解释，不是在标注术语
        glossary = self.harvest("价差/差额（diferencial：价差/差额）保护着人们。")
        self.assertEqual(dict(glossary), {})

    def test_skips_long_descriptive_run(self):
        # 词组必须紧邻括号；「这里用差动保护器」截成「动保护器」比不学更糟
        glossary = self.harvest("这里用差动保护器（diferencial），它跳闸很灵敏。")
        self.assertEqual(dict(glossary), {})

    def test_evicts_oldest_over_limit(self):
        from collections import OrderedDict

        from aula.translate import harvest_terms

        glossary = OrderedDict()
        words = ["进度表", "工期表", "风险表", "材料表", "人员表", "验收表"]
        for index in range(9):
            harvest_terms("%s（termino%d）说明。" % (words[index % 6], index), glossary, limit=4)
        self.assertEqual(len(glossary), 4)
        self.assertIn("termino8", glossary)
        self.assertNotIn("termino0", glossary, "最久没用的先淘汰")

    def test_glossary_injected_into_prompt(self):
        from collections import OrderedDict

        class Spy:
            def __init__(self):
                self.systems = []

            def chat(self, messages, **kwargs):
                self.systems.append(messages[0]["content"])
                return "译文"

        llm = Spy()
        translator = Translator(llm, "es", "zh")
        translator.glossary = OrderedDict({"consignación": "断电挂牌"})
        translator.translate("otra frase cualquiera.")
        self.assertIn("固定译名", llm.systems[0])
        self.assertIn("consignación→断电挂牌", llm.systems[0])


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


class GoogleFreePathOrderTest(unittest.TestCase):
    """端点顺序必须按实测可用性排：clients5 两条路径在前，废弃主机垫底。"""

    def test_order_and_fallback(self):
        urls = []

        class Opener:
            def open(self, request, timeout=0):
                urls.append(request.full_url)
                # 第一条路径返回拦截页，第二条返回嵌套数组（真实现场见过的形状）
                if len(urls) == 1:
                    payload = "<html>Sorry...</html>"
                else:
                    payload = json.dumps(
                        [[["截止日期是五周。", "El plazo es de cinco semanas.", None, None, 3]], None, "es"]
                    )
                return _FakeResponse(payload)

        with mock.patch("escucha.mt.opener_for", return_value=Opener()) if False else mock.patch(
            "aula.mt.opener_for", return_value=Opener()
        ):
            text = mt.GoogleFreeMT().translate("El plazo es de cinco semanas.", "es", "zh")

        self.assertEqual(text, "截止日期是五周。")
        self.assertEqual(len(urls), 2, "第一条被拦后才应走第二条")
        self.assertIn("clients5.google.com/translate_a/t?", urls[0])
        self.assertIn("client=dict-chrome-ex", urls[0])
        self.assertIn("clients5.google.com/translate_a/single?", urls[1])

    def test_all_paths_blocked_raises(self):
        class Opener:
            def open(self, request, timeout=0):
                return _FakeResponse("<html>Sorry...</html>")

        with mock.patch("aula.mt.opener_for", return_value=Opener()):
            with self.assertRaises(mt.MTError):
                mt.GoogleFreeMT().translate("Hola.", "es", "zh")

    def test_real_endpoint_live(self):
        """真连一次 Google：确认首选路径当前真的通（无网络时跳过）。"""
        try:
            text = mt.GoogleFreeMT(timeout=12).translate("El plazo es de cinco semanas.", "es", "zh")
        except mt.MTError as exc:
            self.skipTest("网络不可用或被限流：%s" % str(exc)[:80])
        self.assertIn("五周", text)


class _FakeResponse:
    def __init__(self, payload: str) -> None:
        self._payload = payload.encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False
