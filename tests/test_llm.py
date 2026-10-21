"""LLM 客户端的限流冷却测试。

真实场景：Gemini 免费层配额用尽时会返回 429。如果没有冷却，课堂上每一句话
都会先去撞一次 429 再降级——等于每句都白等一个来回。冷却期内应该直接快速
失败，让上层立刻走备用链路。
"""

from __future__ import annotations

import io
import sys
import urllib.error
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aula.llm import LLMClient, LLMError, LLMRateLimited  # noqa: E402


def http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        url="https://example.invalid/v1/chat/completions",
        code=code,
        msg="boom",
        hdrs=None,
        fp=io.BytesIO(b'{"error": {"message": "quota exceeded"}}'),
    )


class CooldownTest(unittest.TestCase):
    def setUp(self):
        self.client = LLMClient(
            base_url="https://example.invalid/v1",
            api_key="k",
            model="fake",
            timeout=5,
            retries=0,
            cooldown_seconds=30.0,
        )

    def test_429_enters_cooldown_and_next_call_fails_fast(self):
        opener = mock.Mock()
        opener.open.side_effect = http_error(429)
        with mock.patch("aula.llm.opener_for", return_value=opener):
            with self.assertRaises(LLMError) as first:
                self.client.chat([{"role": "user", "content": "hi"}])
        self.assertIsInstance(first.exception, LLMRateLimited)
        self.assertIn("429", str(first.exception))
        self.assertTrue(self.client._in_cooldown())

        # 冷却期内不再发请求
        calls_before = opener.open.call_count
        with self.assertRaises(LLMError) as second:
            self.client.chat([{"role": "user", "content": "hi again"}])
        self.assertEqual(opener.open.call_count, calls_before)
        self.assertIsInstance(second.exception, LLMRateLimited)
        self.assertIn("冷却", str(second.exception))

    def test_cooldown_expires_and_request_goes_through(self):
        import time

        self.client.cooldown_seconds = 0.05
        opener = mock.Mock()
        opener.open.side_effect = http_error(429)
        with mock.patch("aula.llm.opener_for", return_value=opener):
            with self.assertRaises(LLMError):
                self.client.chat([{"role": "user", "content": "hi"}])
            self.assertTrue(self.client._in_cooldown())
            time.sleep(0.08)
            self.assertFalse(self.client._in_cooldown())

            opener.open.side_effect = None
            opener.open.return_value.__enter__ = lambda _self: io.BytesIO(
                b'{"choices": [{"message": {"content": "ok"}}]}'
            )
            opener.open.return_value.__exit__ = lambda *args: False
            reply = self.client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(reply, "ok")

    def test_404_does_not_enter_cooldown(self):
        opener = mock.Mock()
        opener.open.side_effect = http_error(404)
        with mock.patch("aula.llm.opener_for", return_value=opener):
            with self.assertRaises(LLMError):
                self.client.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(self.client._in_cooldown())

    def test_zero_cooldown_disabled(self):
        client = LLMClient(base_url="https://example.invalid/v1", api_key="k", retries=0, cooldown_seconds=0)
        opener = mock.Mock()
        opener.open.side_effect = http_error(429)
        with mock.patch("aula.llm.opener_for", return_value=opener):
            for _ in range(3):
                with self.assertRaises(LLMError):
                    client.chat([{"role": "user", "content": "hi"}])
        self.assertFalse(client._in_cooldown())
        self.assertEqual(opener.open.call_count, 3)


if __name__ == "__main__":
    unittest.main()
