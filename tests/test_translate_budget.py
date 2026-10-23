"""翻译的 token 预算：推理模型必须留思考余量。

真实故障：DeepSeek-V4.1-Flash 接进来后，大半句子返回空译文——字幕上就是空白。
根因是 `max_tokens` 把 reasoning_token 也算在里面，而预算按句子字符数给
（69 字符 → 169），实测单句思考量在 69–207 token 之间波动、与句长无关，
于是 content 被挤出上限（finish_reason=length），`_strip_noise` 拿到空串。

修法：`client.reasoning_effort` 非空时给预算加 400 余量。这两条测试守的是
「有余量」和「非推理模型不被顺带放大」。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aula.llm import LLMClient  # noqa: E402
from aula.translate import Translator  # noqa: E402

SHORT = "La vivienda tiene dos tipos de corriente."


class TranslateBudgetTest(unittest.TestCase):
    def _budget(self, effort: str, text: str = SHORT) -> int:
        captured: dict = {}
        client = LLMClient(
            base_url="https://example.invalid/v1",
            api_key="k",
            model="fake",
            reasoning_effort=effort,
        )

        def fake_chat(messages, **kwargs):  # noqa: ANN001, ANN003
            captured.update(kwargs)
            return "译文"

        client.chat = fake_chat  # type: ignore[method-assign]
        Translator(client).translate(text)
        self.assertIn("max_tokens", captured, "翻译必须显式给 max_tokens")
        return captured["max_tokens"]

    def test_reasoning_model_gets_thinking_headroom(self):
        plain = self._budget("")
        with_effort = self._budget("low")
        self.assertEqual(plain, min(800, 100 + len(SHORT)))
        # 至少多出 400 的思考余量，否则 reasoning 会吃掉译文预算
        self.assertGreaterEqual(with_effort - plain, 400)
        self.assertEqual(with_effort, min(1200, plain + 400))

    def test_headroom_is_capped(self):
        long_text = "La " + "instalación eléctrica " * 80
        budget = self._budget("low", long_text)
        self.assertLessEqual(budget, 1200, "上限不能无限涨，否则长句会烧满额度")

    def test_non_reasoning_model_budget_unchanged(self):
        # Gemini / Groq 这类不是推理模型，多给预算只会让它们有机会铺开闲写
        for text in (SHORT, "La " + "instalación " * 40):
            budget = self._budget("", text)
            # translate() 会先 strip，长度要按 strip 后算
            self.assertEqual(budget, min(800, 100 + len(text.strip())))


if __name__ == "__main__":
    unittest.main()
