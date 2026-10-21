"""真实 Gemini 联调测试（有密钥才跑，CI 里自动跳过）。

    export GEMINI_API_KEY=...
    python3 -m unittest tests.test_live_gemini -v

用系统自带的 `say` 合成一段西班牙语，转成 16k 单声道 wav，再喂给 GeminiASR。
这样不用准备音频素材，也能验证"真音频 → 真转写"这条路。

没有密钥时整个用例 skip，不会把 CI 打红，也不会偷偷消耗别人的额度。
"""

import asyncio
import os
import time
import shutil
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aula.asr import ASRError, GeminiASR  # noqa: E402
from aula.llm import LLMClient, LLMError  # noqa: E402

KEY = os.environ.get("GEMINI_API_KEY") or os.environ.get("AULA_LLM_API_KEY") or ""
ASR_MODEL = os.environ.get("AULA_TEST_ASR_MODEL", "gemini-3.8-flash")
BASE = "https://generativelanguage.googleapis.com/v1beta"

SPANISH = (
    "Hola a todos. Vamos a ver el plan general de construcción de la instalación eléctrica. "
    "La prevención de riesgos laborales es fundamental."
)


def synth_wav(text: str) -> bytes:
    """用 macOS 的 say 合成语音，返回 16k 单声道 PCM。非 macOS 直接跳过。"""
    if not shutil.which("say") or not shutil.which("afconvert"):
        raise unittest.SkipTest("需要 macOS 的 say / afconvert 才能合成测试音频")
    with tempfile.TemporaryDirectory() as tmp:
        aiff = Path(tmp) / "a.aiff"
        wav = Path(tmp) / "a.wav"
        subprocess.run(["say", "-v", "Eddy", "-o", str(aiff), text], check=True)
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
            check=True,
        )
        with wave.open(str(wav)) as handle:
            return handle.readframes(handle.getnframes())


def retry_or_skip(fn, attempts=4):
    """上游 503（高峰过载）不该把测试判红——重试几次，仍然 503 就跳过。"""
    last = None
    for index in range(attempts):
        try:
            return fn()
        except (LLMError, ASRError) as exc:
            message = str(exc)
            if "503" not in message and "429" not in message and "暂时不可用" not in message:
                raise
            last = exc
            time.sleep(2.0 * (index + 1))
    raise unittest.SkipTest("上游暂时过载（503），本次跳过：%s" % str(last)[:80])


@unittest.skipUnless(KEY, "未设置 GEMINI_API_KEY，跳过真实联调")
class LiveGeminiTest(unittest.TestCase):
    def test_transcribes_spanish(self):
        pcm = synth_wav(SPANISH)
        asr = GeminiASR(base_url="", api_key=KEY, model=ASR_MODEL)
        result = retry_or_skip(lambda: asyncio.run(asr.transcribe(pcm, 16000, "es")))
        text = result.text.lower()
        self.assertIn("plan general", text)
        self.assertIn("riesgos laborales", text)
        # 识别结果应该覆盖大部分内容，不该只蹦出一个词
        self.assertGreater(len(result.text), 60)

    def test_translates_to_chinese(self):
        client = LLMClient(base_url=BASE + "/openai", api_key=KEY, model=ASR_MODEL, timeout=60, retries=1)
        translated = retry_or_skip(
            lambda: client.chat(
                [
                    {"role": "system", "content": "把下面这句西班牙语翻译成中文，只输出译文。"},
                    {"role": "user", "content": "La prevención de riesgos laborales es fundamental."},
                ],
                max_tokens=256,
            )
        )
        self.assertTrue(translated)
        self.assertTrue(any("\u4e00" <= ch <= "\u9fff" for ch in translated), "译文应包含汉字")

    def test_chat_returns_text(self):
        client = LLMClient(base_url=BASE + "/openai", api_key=KEY, model=ASR_MODEL, timeout=60, retries=1)
        reply = retry_or_skip(
            lambda: client.chat([{"role": "user", "content": "只回复两个字：可用"}], max_tokens=256)
        )
        self.assertTrue(reply.strip(), "空回复说明密钥或模型有问题")


if __name__ == "__main__":
    unittest.main()
