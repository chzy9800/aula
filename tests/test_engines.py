"""引擎测试：ASR 工厂、WAV 封装、翻译清洗、总结规范化。"""

import asyncio
import struct
import sys
import unittest
import wave
from io import BytesIO
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aula import config  # noqa: E402
from aula.asr import MockASR, OpenAICompatASR, pcm_to_wav  # noqa: E402
from aula.summarize import normalize, render_utterances  # noqa: E402
from aula.translate import Translator, _strip_noise  # noqa: E402

pytest_plugins = ()  # 保持 unittest 兼容


class WavTest(unittest.TestCase):
    def test_header_is_valid(self):
        pcm = struct.pack("<%dh" % 1600, *([0] * 1600))
        wav = pcm_to_wav(pcm, 16000)
        with wave.open(BytesIO(wav), "rb") as handle:
            self.assertEqual(handle.getnchannels(), 1)
            self.assertEqual(handle.getsampwidth(), 2)
            self.assertEqual(handle.getframerate(), 16000)
            self.assertEqual(handle.getnframes(), 1600)


class MockASRTest(unittest.TestCase):
    def test_returns_sentence(self):
        asr = MockASR()
        result = asyncio.run(asr.transcribe(b"\x00\x00" * 8000, 16000, "es"))
        self.assertTrue(result.text)
        self.assertEqual(result.provider, "mock")

    def test_rotates(self):
        asr = MockASR()
        first = asyncio.run(asr.transcribe(b"\x00\x00" * 8000, 16000, "es")).text
        second = asyncio.run(asr.transcribe(b"\x00\x00" * 8000, 16000, "es")).text
        self.assertNotEqual(first, second)


class ProviderPickTest(unittest.TestCase):
    def test_explicit_provider(self):
        self.assertEqual(config.Settings(asr_provider="mock").pick_asr_provider(), "mock")

    def test_auto_prefers_openai_when_key_present(self):
        settings = config.Settings(asr_provider="auto", asr_api_key="sk-x", asr_base_url="https://x/v1")
        self.assertEqual(settings.pick_asr_provider(), "openai")

    def test_openai_requires_base_url(self):
        with self.assertRaises(Exception):
            OpenAICompatASR(base_url="", api_key="k")

    def test_default_models(self):
        self.assertEqual(config.Settings(asr_provider="openai").resolved_asr_model(), "whisper-1")
        self.assertEqual(config.Settings(asr_provider="whisper", asr_model="").resolved_asr_model(), "medium")


class TranslatorTest(unittest.TestCase):
    class FakeClient:
        def __init__(self, reply="译文内容"):
            self.reply = reply
            self.calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            return self.reply

    def test_translate_and_cache(self):
        fake = self.FakeClient()
        translator = Translator(fake, "es", "zh")
        self.assertEqual(translator.translate("Hola"), "译文内容")
        self.assertEqual(translator.translate("Hola"), "译文内容")
        self.assertEqual(fake.calls, 1, "重复句子应命中缓存")

    def test_same_language_is_noop(self):
        translator = Translator(self.FakeClient(), "zh", "zh")
        self.assertFalse(translator.enabled)
        self.assertEqual(translator.translate("原样返回"), "原样返回")

    def test_prefix_stripped(self):
        self.assertEqual(_strip_noise("译文：大家好"), "大家好")
        self.assertEqual(_strip_noise("「大家好」"), "大家好")

    def test_empty_input(self):
        translator = Translator(self.FakeClient(), "es", "zh")
        self.assertEqual(translator.translate(""), "")

    def test_llm_failure_returns_empty(self):
        from aula.llm import LLMError

        class Broken:
            def chat(self, *args, **kwargs):
                raise LLMError("down")

        translator = Translator(Broken(), "es", "zh")
        self.assertEqual(translator.translate("Hola"), "")


class SummarizeTest(unittest.TestCase):
    def test_render_utterances(self):
        text = render_utterances(
            [
                {"source": "Hola", "target": "你好", "start_ms": 65000},
                {"source": "Adiós", "target": "", "start_ms": 70000},
            ]
        )
        self.assertIn("[01:05] Hola", text)
        self.assertIn("译：你好", text)
        self.assertIn("[01:10] Adiós", text)

    def test_normalize_variants(self):
        content = normalize(
            {
                "overview": "总览",
                "points": ["纯字符串要点"],
                "terms": [{"term": "plan"}, {"term": "Plan"}, {"term": "obra"}],
                "todos": "单条待办",
                "questions": ["问题一"],
                "emphasis": "重点",
            }
        )
        self.assertEqual(content["points"][0]["detail"], "纯字符串要点")
        self.assertEqual(len(content["terms"]), 2, "术语应按小写去重")
        self.assertEqual(content["todos"], ["单条待办"])
        self.assertEqual(content["emphasis"], ["重点"])

    def test_normalize_empty(self):
        content = normalize({})
        for key in ("points", "terms", "todos", "questions", "emphasis"):
            self.assertEqual(content[key], [])


if __name__ == "__main__":
    unittest.main()
