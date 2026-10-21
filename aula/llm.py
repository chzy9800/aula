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


# chat_json 解析失败时的纠偏提示。只提格式要求，不重复原任务——
# 原消息已在历史里，再抄一遍只会让输入变长。
_JSON_NUDGE = ("上一条回复不是合法的 JSON。请只输出一个 JSON 对象："
               "不要思考过程、不要 Markdown 围栏、不要任何其他文字。")


@dataclass
class LLMClient:
    base_url: str
    api_key: str = ""
    model: str = "qwen3:8b"
    timeout: int = 120
    retries: int = 2
    temperature: float = 0.2

    # 推理模型的思考档位（GLM-5 / DeepSeek-V4 这类 only_reasoning 模型实测：
    # 默认 high 档下每句翻译要先烧几百思考 token，实时场景等不起）。留空则不
    # 发送该参数——老接口（Gemini 等）不认识它会直接 400。
    reasoning_effort: str = ""

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
        if self.reasoning_effort:
            payload["reasoning_effort"] = self.reasoning_effort
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
        raw = self.chat(messages, **kwargs)
        parsed = extract_json(raw)
        if parsed is not None:
            return parsed
        # 偶发失败（实测 GLM-5.3-Flash 走本地网关时约 1/20）：思考链混进正文，
        # 或输出被截断。带一句纠偏重试一次，比让整块内容报废便宜得多。
        nudge = list(messages) + [
            {"role": "user", "content": _JSON_NUDGE},
        ]
        retry_raw = self.chat(nudge, **kwargs)
        parsed = extract_json(retry_raw)
        if parsed is None:
            raise LLMError(
                "模型没有返回可解析的 JSON（已重试）；首次返回开头：%s"
                % raw[:120].replace("\n", " ")
            )
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
    """从可能带围栏、思考链或截断的回复里抠出 JSON 对象。

    三个真实坑（都是实测遇到的，不是假想）：
    1. 围栏：```json ... ``` 包着；
    2. 思考链混进正文：推理模型偶发不把思考分流到 reasoning_content，
       于是 content 里先有一段带花括号的推敲、最后才是答案——**第一个
       花括号属于推敲**，只试第一个必然失败；
    3. 截断：输出到一半被上限切断，JSON 少一半闭合括号。

    所以：围栏优先；然后按顺序尝试文本里**每一个** 完整 {...}；都失败时
    对未闭合的尾部做一次括号修补。
    """
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
        if not candidate:
            continue
        try:
            loaded = json.loads(candidate)
            if isinstance(loaded, dict):
                return loaded
        except json.JSONDecodeError:
            pass
        start = candidate.find("{")
        while start != -1:
            found = _scan_object(candidate, start)
            if found is not None:
                return found
            # 这个起点扫不出完整对象：未闭合（截断）就先修补，不行再找下一个 {
            repaired = _repair_truncated(candidate[start:])
            if repaired is not None:
                return repaired
            start = candidate.find("{", start + 1)
    return None


def _scan_object(text: str, start: int) -> Optional[Dict[str, Any]]:
    """从 start 处的 '{' 开始找第一个括号配平的片段并解析。"""
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        ch = text[index]
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
                    loaded = json.loads(text[start : index + 1])
                except json.JSONDecodeError:
                    return None
                return loaded if isinstance(loaded, dict) else None
    return None


def _repair_truncated(fragment: str) -> Optional[Dict[str, Any]]:
    """给被截断的 JSON 补上未闭合的字符串与括号。

    只做"补右括号"这一种修补——截断永远丢尾巴，不会多出内容。补完还解析不了
    （比如正好切断在键名中间）就认输，交给上层重试。
    """
    if not fragment.lstrip().startswith("{"):
        return None
    stack: List[str] = []
    in_string = False
    escape = False
    for ch in fragment:
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
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if stack:
                stack.pop()
    if not stack and not in_string:
        return None          # 本来就配平，是别的问题，不是截断
    patched = fragment
    if in_string:
        patched += '"'
    patched += "".join(reversed(stack))
    try:
        loaded = json.loads(patched)
    except json.JSONDecodeError:
        return None
    return loaded if isinstance(loaded, dict) else None


def client_for(settings, *, model: Optional[str] = None,
               timeout: Optional[int] = None,
               retries: Optional[int] = None) -> LLMClient:
    """从一份 Settings 构造客户端。

    为什么收一个 settings 而不是逐个传 base_url/api_key：调用点有十几处，
    每加一个供应商相关参数（如 reasoning_effort）就要改一遍——漏一处的症状是
    「同一个配置，大纲走 low 档、翻译走 high 档」，极难查。这里做单点。
    """
    return LLMClient(
        base_url=settings.llm_base_url,
        api_key=settings.llm_api_key,
        model=model or settings.resolved_summary_model(),
        timeout=120 if timeout is None else timeout,
        retries=1 if retries is None else retries,
        reasoning_effort=getattr(settings, "llm_reasoning_effort", "") or "",
    )
