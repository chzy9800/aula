"""语音转写：可插拔 provider。

三种实现覆盖三种现实：
  * ``mock``   —— 没有模型也能把整条链路跑通（演示、测试、录屏）
  * ``openai`` —— 任意兼容 ``/v1/audio/transcriptions`` 的云端服务，零额外依赖
  * ``whisper``—— 本地 faster-whisper，课堂内容不出机器

统一约定：输入是 16kHz 单声道 PCM16 的**一整句**（前端 VAD 已经切好），
输出是这一句的文本。不做流式增量识别——因为"逐句出字"比"逐字出字"在
课堂场景更合用：老师说完整句，翻译才有上下文。
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from dataclasses import dataclass
from typing import Any, Dict, Optional

from .net import opener_for


class ASRError(RuntimeError):
    pass


@dataclass
class ASRResult:
    text: str
    language: Optional[str] = None
    confidence: Optional[float] = None
    provider: str = ""


def pcm_to_wav(pcm: bytes, sample_rate: int) -> bytes:
    """给裸 PCM 套一个 WAV 头。云端接口只认容器格式，本地模型不需要。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return buffer.getvalue()


class BaseASR:
    name = "base"

    async def transcribe(self, pcm: bytes, sample_rate: int, lang: str) -> ASRResult:
        raise NotImplementedError

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name}


class MockASR(BaseASR):
    """轮播课堂句子。用来验证链路，不产生真实识别结果。"""

    name = "mock"

    SENTENCES = [
        "Vamos a ver la organización de las etapas de ejecución.",
        "El plan general de construcción recoge toda la documentación técnica.",
        "Primero se hace el croquis y después los planos de detalle.",
        "Hay que asignar los recursos humanos y las tareas de cada actividad.",
        "El acopio de materiales evita que la obra se detenga.",
        "El plazo es el tiempo del que disponemos para ejecutar la instalación.",
        "La prevención de riesgos laborales debe estar presente todo el proceso.",
        "Para terminar, se realizan las pruebas funcionales de la instalación.",
    ]

    def __init__(self) -> None:
        self._cursor = 0
        self._lock = threading.Lock()

    async def transcribe(self, pcm: bytes, sample_rate: int, lang: str) -> ASRResult:
        duration = max(0.2, len(pcm) / 2.0 / max(sample_rate, 1))
        await asyncio.sleep(min(0.5, 0.15 + duration * 0.04))
        with self._lock:
            text = self.SENTENCES[self._cursor % len(self.SENTENCES)]
            self._cursor += 1
        return ASRResult(text=text, language="es", confidence=0.95, provider=self.name)

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "note": "演示模式，返回预置句子"}


class OpenAICompatASR(BaseASR):
    """调用任意 OpenAI 兼容的 /audio/transcriptions。"""

    name = "openai"

    def __init__(self, base_url: str, api_key: str, model: str = "whisper-1", timeout: int = 60) -> None:
        if not base_url:
            raise ASRError("云端转写需要配置 asr_base_url")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    async def transcribe(self, pcm: bytes, sample_rate: int, lang: str) -> ASRResult:
        return await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate, lang)

    def _transcribe_sync(self, pcm: bytes, sample_rate: int, lang: str) -> ASRResult:
        boundary = "----escucha" + uuid.uuid4().hex
        wav = pcm_to_wav(pcm, sample_rate)
        parts = []

        def field(name: str, value: str) -> None:
            parts.append(
                ("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n" % (boundary, name, value)).encode("utf-8")
            )

        field("model", self.model)
        field("response_format", "json")
        if lang and lang != "auto":
            field("language", lang)
        parts.append(
            (
                "--%s\r\nContent-Disposition: form-data; name=\"file\"; filename=\"segment.wav\"\r\n"
                "Content-Type: audio/wav\r\n\r\n" % boundary
            ).encode("utf-8")
        )
        parts.append(wav)
        parts.append(("\r\n--%s--\r\n" % boundary).encode("utf-8"))
        body = b"".join(parts)

        headers = {"Content-Type": "multipart/form-data; boundary=%s" % boundary}
        if self.api_key:
            headers["Authorization"] = "Bearer %s" % self.api_key
        request = urllib.request.Request(
            self.base_url + "/audio/transcriptions", data=body, headers=headers, method="POST"
        )
        try:
            with opener_for(self.base_url).open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise ASRError("转写接口返回 %d：%s" % (exc.code, detail)) from exc
        except urllib.error.URLError as exc:
            raise ASRError("无法连接转写接口：%s" % exc.reason) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ASRError("转写接口返回的不是 JSON：%s" % raw[:150]) from exc
        if isinstance(data, dict) and data.get("text") is not None:
            return ASRResult(
                text=str(data.get("text", "")).strip(),
                language=data.get("language") or lang,
                confidence=data.get("confidence"),
                provider=self.name,
            )
        raise ASRError("转写接口响应缺少 text 字段")

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "fallback": self.fallback_model,
            "base_url": self.base_url,
        }


class WhisperLocalASR(BaseASR):
    """本地 faster-whisper。首次调用会下载模型（small ≈ 480MB）。"""

    name = "whisper"

    _model_lock = threading.Lock()
    _models: Dict[str, Any] = {}

    def __init__(self, model_size: str = "small", device: str = "auto", compute_type: str = "int8") -> None:
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type

    def _load(self) -> Any:
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as exc:
            raise ASRError(
                "未安装 faster-whisper。安装：pip install 'escucha[local]'，或改用云端转写。"
            ) from exc
        key = "%s|%s|%s" % (self.model_size, self.device, self.compute_type)
        with self._model_lock:
            if key not in self._models:
                self._models[key] = WhisperModel(
                    self.model_size, device=self.device, compute_type=self.compute_type
                )
            return self._models[key]

    async def transcribe(self, pcm: bytes, sample_rate: int, lang: str) -> ASRResult:
        return await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate, lang)

    def _transcribe_sync(self, pcm: bytes, sample_rate: int, lang: str) -> ASRResult:
        try:
            import numpy as np  # type: ignore
        except ImportError as exc:
            raise ASRError("本地转写需要 numpy，请安装 'escucha[local]'") from exc

        model = self._load()
        audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
        if sample_rate != 16000:
            # faster-whisper 内部按 16k 处理，采样率不符时直接报错更安全
            raise ASRError("本地转写要求 16kHz 输入，当前 %d Hz" % sample_rate)

        with self._model_lock:
            segments, info = model.transcribe(
                audio,
                language=None if lang in ("", "auto") else lang,
                vad_filter=True,
                beam_size=1,
                condition_on_previous_text=False,
            )
            text = " ".join(segment.text.strip() for segment in segments).strip()
        return ASRResult(
            text=text,
            language=getattr(info, "language", lang),
            confidence=getattr(info, "language_probability", None),
            provider=self.name,
        )

    def describe(self) -> Dict[str, Any]:
        return {"provider": self.name, "model": self.model_size, "device": self.device}


class GeminiASR(BaseASR):
    """Google Gemini 原生内联音频转写。

    走 ``models/{model}:generateContent`` 而不是 OpenAI 兼容层，因为兼容层
    没有 ``/audio/transcriptions``。免费层就够用：一段 5 秒音频约 30KB，
    远小于内联请求的大小上限。
    """

    name = "gemini"

    PROMPT = (
        "逐字转写这段课堂音频（语言：{lang}）。只输出转写文本，"
        "不要翻译、不要加解释、不要总结。听不清的部分写 [听不清]；"
        "如果没有人声，输出空字符串。"
    )

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str = "gemini-3.8-flash",
        timeout: int = 60,
        fallback_model: str = "gemini-3.5-flash",
    ) -> None:
        self.base_url = (base_url or "https://generativelanguage.googleapis.com/v1beta").rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        # 实测 3.8-flash 高峰期会返回 503 high demand，备用模型能接住
        self.fallback_model = fallback_model

    async def transcribe(self, pcm: bytes, sample_rate: int, lang: str) -> ASRResult:
        return await asyncio.to_thread(self._transcribe_sync, pcm, sample_rate, lang)

    def _transcribe_sync(self, pcm: bytes, sample_rate: int, lang: str) -> ASRResult:
        if not self.api_key:
            raise ASRError("Gemini 转写需要密钥：设置 GEMINI_API_KEY 或 --asr-api-key")

        models = [self.model]
        if self.fallback_model and self.fallback_model != self.model:
            models.append(self.fallback_model)

        last_error: Optional[Exception] = None
        for attempt, model in enumerate(models):
            try:
                return self._once(pcm, sample_rate, lang, model)
            except ASRError as exc:
                last_error = exc
                retryable = "503" in str(exc) or "429" in str(exc)
                if not retryable and attempt == 0 and len(models) > 1:
                    # 模型名失效之类的问题，换备用模型也可能不行，但值得一试
                    continue
                if not retryable:
                    raise
            if attempt + 1 < len(models):
                time.sleep(1.0)
        raise last_error or ASRError("Gemini 转写失败")

    def _once(self, pcm: bytes, sample_rate: int, lang: str, model: str) -> ASRResult:
        from .translate import LANG_NAMES

        wav = pcm_to_wav(pcm, sample_rate)
        payload = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": self.PROMPT.format(lang=LANG_NAMES.get(lang, lang))},
                        {
                            "inline_data": {
                                "mime_type": "audio/wav",
                                "data": base64.b64encode(wav).decode("ascii"),
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 2048},
        }
        url = "%s/models/%s:generateContent?key=%s" % (self.base_url, model, self.api_key)
        request = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with opener_for(self.base_url).open(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            hint = "（免费层限速，稍后重试）" if exc.code == 429 else ""
            raise ASRError("Gemini 返回 %d%s：%s" % (exc.code, hint, detail)) from exc
        except urllib.error.URLError as exc:
            raise ASRError("无法连接 Gemini：%s" % exc.reason) from exc

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ASRError("Gemini 响应不是 JSON：%s" % raw[:150]) from exc

        candidates = data.get("candidates") or []
        if not candidates:
            blocked = (data.get("promptFeedback") or {}).get("blockReason")
            if blocked:
                raise ASRError("Gemini 拒绝了该请求：%s" % blocked)
            return ASRResult(text="", language=lang, provider=self.name)
        parts = ((candidates[0].get("content") or {}).get("parts")) or []
        text = " ".join(str(part.get("text", "")) for part in parts if isinstance(part, dict)).strip()
        return ASRResult(text=text, language=lang, confidence=None, provider=self.name)

    def describe(self) -> Dict[str, Any]:
        return {
            "provider": self.name,
            "model": self.model,
            "fallback": self.fallback_model,
            "base_url": self.base_url,
        }


def build_asr(settings: Any) -> BaseASR:
    """按配置构造 provider。auto 的解析规则见 Settings.pick_asr_provider。"""
    provider = settings.pick_asr_provider()
    if provider == "openai":
        return OpenAICompatASR(
            base_url=settings.asr_base_url,
            api_key=settings.asr_api_key,
            model=settings.resolved_asr_model(),
        )
    if provider == "whisper":
        return WhisperLocalASR(model_size=settings.resolved_asr_model())
    if provider == "gemini":
        return GeminiASR(
            base_url=settings.asr_base_url or "https://generativelanguage.googleapis.com/v1beta",
            api_key=settings.asr_api_key,
            model=settings.asr_model or "gemini-3.8-flash",
        )
    return MockASR()


class TimedASR:
    """包一层计时，方便把延迟回传给前端显示。"""

    def __init__(self, inner: BaseASR) -> None:
        self.inner = inner

    async def transcribe(self, pcm: bytes, sample_rate: int, lang: str) -> "tuple[ASRResult, int]":
        started = time.monotonic()
        result = await self.inner.transcribe(pcm, sample_rate, lang)
        elapsed = int((time.monotonic() - started) * 1000)
        return result, elapsed

    def describe(self) -> Dict[str, Any]:
        return self.inner.describe()
