"""LLM 客户端的限流冷却测试。

真实场景：Gemini 免费层配额用尽时会返回 429。如果没有冷却，课堂上每一句话
都会先去撞一次 429 再降级——等于每句都白等一个来回。冷却期内应该直接快速
失败，让上层立刻走备用链路。
"""

from __future__ import annotations

import io
import json
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


def _reply_opener(reply: bytes):
    """让 opener.open 返回一段固定响应体的假 opener。"""
    opener = mock.Mock()
    opener.open.return_value.__enter__ = lambda _self: io.BytesIO(reply)
    opener.open.return_value.__exit__ = lambda *args: False
    return opener


class ReasoningEffortTest(unittest.TestCase):
    """推理模型思考档：只在设了的时候下发，别去吵不认这个参数的接口。"""

    def test_effort_sent_only_when_set(self):
        reply = b'{"choices": [{"message": {"content": "ok"}}]}'
        for effort, expect in (("low", True), ("high", True), ("", False)):
            client = LLMClient(
                base_url="https://example.invalid/v1", api_key="k",
                model="cn:glm-5.3-flash", retries=0, reasoning_effort=effort,
            )
            opener = _reply_opener(reply)
            with mock.patch("aula.llm.opener_for", return_value=opener):
                client.chat([{"role": "user", "content": "hi"}])
            body = json.loads(opener.open.call_args[0][0].data.decode("utf-8"))
            self.assertEqual(("reasoning_effort" in body), expect, effort)
            if expect:
                self.assertEqual(body["reasoning_effort"], effort)

    def test_client_for_threads_setting(self):
        from aula import config
        from aula.llm import client_for

        settings = config.Settings(
            llm_base_url="http://127.0.0.1:7864/v1",
            llm_api_key="wbk_test",
            llm_model="cn:glm-5.3-flash",
            llm_reasoning_effort="low",
        )
        client = client_for(settings, timeout=90)
        self.assertEqual(client.base_url, "http://127.0.0.1:7864/v1")
        self.assertEqual(client.api_key, "wbk_test")
        self.assertEqual(client.reasoning_effort, "low")
        self.assertEqual(client.timeout, 90)
        self.assertEqual(client.retries, 1)


class ChatJsonRetryTest(unittest.TestCase):
    """解析失败时带一句纠偏重试——实测本地网关约 1/20 概率需要它。"""

    def _client(self, replies):
        """replies 是模型正文；这里包上 chat.completion 信封再喂给假 opener。"""
        client = LLMClient(base_url="https://example.invalid/v1", api_key="k",
                           model="fake", retries=0)
        opener = mock.Mock()
        handles = [
            io.BytesIO(json.dumps(
                {"choices": [{"message": {"content": r}}]}
            ).encode("utf-8"))
            for r in replies
        ]
        opener.open.side_effect = handles
        return client, opener

    def test_retry_with_nudge_recovers(self):
        client, opener = self._client([
            '我先想想……（思考完但没输出 JSON）',      # 首次：不可解析
            '{"points": ["甲"]}',                  # 重试：干净 JSON
        ])
        with mock.patch("aula.llm.opener_for", return_value=opener):
            parsed = client.chat_json([{"role": "user", "content": "抽要点"}])
        self.assertEqual(parsed, {"points": ["甲"]})
        self.assertEqual(opener.open.call_count, 2)
        # 第二次请求里带上了纠偏提示
        second = json.loads(opener.open.call_args[0][0].data.decode("utf-8"))
        self.assertIn("不是合法的 JSON", second["messages"][-1]["content"])

    def test_both_fail_raises_with_snippet(self):
        client, opener = self._client([
            '不是JSON',
            '还不是JSON',
        ])
        with mock.patch("aula.llm.opener_for", return_value=opener):
            with self.assertRaises(LLMError) as ctx:
                client.chat_json([{"role": "user", "content": "抽要点"}])
        self.assertIn("已重试", str(ctx.exception))
        self.assertIn("不是JSON", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
