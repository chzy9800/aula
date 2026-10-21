"""OpenAI 兼容客户端（标准库实现），供翻译与总结共用。

实时场景对它有额外要求：超时要短、失败不能把整节课拖住，所以调用方都
包了 try/except，把异常降级成"这一句没翻出来"，而不是让 WebSocket 断开。
"""

from __future__ import annotations

import json
import socket
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from .net import opener_for


class LLMError(RuntimeError):
    pass


class LLMRateLimited(LLMError):
    """429 限速 / 503 过载，或处于限流冷却期。

    单独一个类型，调用方才能区分"接口挂了"和"接口被限流"——后者值得降级，
    前者可能是配置错误。测试也靠它判断该跳过还是该报错。
    """


@dataclass
class LLMClient:
    base_url: str
    api_key: str = ""
    model: str = "qwen3:8b"
    timeout: int = 120
    retries: int = 2
    temperature: float = 0.2

    # 429/503 冷却：实测 Gemini 免费层配额用尽时，每一句都去撞一次 429 只会
    # 让课堂上的每一句话都多等一个来回。进入冷却后直接快速失败，让上层走降级。
    cooldown_seconds: float = 60.0
    _cool_until: float = 0.0

    @property
    def endpoint(self) -> str:
        return self.base_url.rstrip("/") + "/chat/completions"

    def _in_cooldown(self) -> bool:
        return time.monotonic() < self._cool_until

    def _enter_cooldown(self) -> None:
        self._cool_until = time.monotonic() + self.cooldown_seconds

    def chat(
        self,
        messages: List[Dict[str, str]],
        model: Optional[str] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
        timeout: Optional[int] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": self.temperature if temperature is None else temperature,
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key

        last_error: Optional[Exception] = None
        for attempt in range(self.retries + 1):
            if self._in_cooldown():
                # 冷却期内不再发起请求：让上层立刻走降级，而不是每句都等一个 429
                raise LLMRateLimited(
                    "接口处于限流冷却期（%.0f 秒内不再重试），已切换到备用链路" % self.cooldown_seconds
                )
            request = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
            try:
                with opener_for(self.endpoint).open(
                    request, timeout=timeout or self.timeout
                ) as response:
                    raw = response.read().decode("utf-8", "replace")
                return _content(raw)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:300]
                if exc.code in (400, 401, 403, 404):
                    raise LLMError("模型接口返回 %d：%s" % (exc.code, detail)) from exc
                # 429 限速与 503 过载都是暂时的：值得等一会儿重试
                if exc.code in (429, 503):
                    last_error = LLMRateLimited(
                        "接口暂时不可用（%d，通常是限速或高峰过载）：%s" % (exc.code, detail)
                    )
                    if attempt == self.retries:
                        self._enter_cooldown()
                else:
                    last_error = LLMError("HTTP %d：%s" % (exc.code, detail))
            except urllib.error.URLError as exc:
                last_error = LLMError("无法连接 %s：%s" % (self.endpoint, exc.reason))
            except socket.timeout:
                last_error = LLMError("模型接口超时（%ss）" % (timeout or self.timeout))
            if attempt < self.retries:
                time.sleep(1.5 * (attempt + 1))
        raise last_error or LLMError("未知错误")

    def chat_json(self, messages: List[Dict[str, str]], **kwargs: Any) -> Dict[str, Any]:
        parsed = extract_json(self.chat(messages, **kwargs))
        if parsed is None:
            raise LLMError("模型没有返回可解析的 JSON")
        return parsed


def _content(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError("响应不是 JSON：%s" % raw[:150]) from exc
    choices = data.get("choices") or []
    if not choices:
        raise LLMError("响应缺少 choices")
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, list):
        content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return (content or "").strip()


def extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从可能带围栏的回复里抠出 JSON 对象。"""
    if not text:
        return None
    candidates = []
    fence_start = text.find("```")
    if fence_start != -1:
        fence_end = text.find("```", fence_start + 3)
        block = text[fence_start + 3 : fence_end if fence_end != -1 else len(text)]
        candidates.append(block.split("\n", 1)[1] if "\n" in block else block)
    candidates.append(text)

    for candidate in candidates:
        candidate = candidate.strip()
        try:
            loaded = json.loads(candidate)
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            pass
        start = candidate.find("{")
        if start == -1:
            continue
        depth = 0
        in_string = False
        escape = False
        for index in range(start, len(candidate)):
            ch = candidate[index]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        loaded = json.loads(candidate[start : index + 1])
                        if isinstance(loaded, dict):
                            return loaded
                    except json.JSONDecodeError:
                        break
    return None
