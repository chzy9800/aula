"""配置：默认值 < 配置文件 < 环境变量 < 命令行参数。

刻意把"配置"和"运行状态"分开：``Settings`` 只描述用户意图（说什么语言、
用哪个模型），运行时状态（当前会话、已收音频）不写回配置。
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, Optional

APP_NAME = "aula"
CONFIG_DIR = Path.home() / ".config" / APP_NAME


def config_path() -> Path:
    """配置文件位置。允许用 AULA_CONFIG 覆盖——测试不该写用户的真实配置。"""
    override = os.environ.get("AULA_CONFIG")
    if override:
        return Path(override).expanduser()
    return CONFIG_DIR / "config.json"

# 支持的语言对：课堂语言 -> 笔记语言。可自由扩展，这里给常用组合。
LANGUAGES = {
    "es": "西班牙语",
    "en": "英语",
    "zh": "中文",
    "fr": "法语",
    "de": "德语",
    "it": "意大利语",
    "pt": "葡萄牙语",
    "ja": "日语",
    "ko": "韩语",
    "ru": "俄语",
    "ar": "阿拉伯语",
}

ASR_PROVIDERS = {
    "auto": "自动（有密钥用云端，否则本地，再否则演示）",
    "mock": "演示模式（不调用任何模型）",
    "openai": "云端 OpenAI 兼容 /audio/transcriptions",
    "whisper": "本地 faster-whisper",
    "gemini": "Google Gemini 原生音频（免费额度可用）",
}

DEFAULT_ASR_MODELS = {
    "openai": "whisper-1",
    "whisper": "small",
    "mock": "mock",
    "gemini": "gemini-3.8-flash",
}


def _env(name: str, default: Any) -> Any:
    return os.environ.get("AULA_" + name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get("AULA_" + name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get("AULA_" + name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    """一份完整配置。字段名即接口字段名，前后端共用。"""

    # —— 服务商预设 ——
    provider: str = ""            # 预设 id，如 gemini / groq / ollama；空则用手填参数

    # —— 语言方向 ——
    source_lang: str = "es"       # 课堂语言（老师说什么）
    target_lang: str = "zh"       # 笔记语言（你要什么）
    translate: bool = True        # 关闭则只转写不翻译

    # —— 转写 ——
    asr_provider: str = "auto"
    asr_model: str = ""
    asr_base_url: str = ""
    asr_api_key: str = ""

    # —— 翻译引擎 ——
    # llm = 用大模型翻译（默认，术语与上下文更好）
    # google / azure / deepl = 专用机器翻译接口（更快、按字符计费、额度更大）
    translate_engine: str = "llm"
    mt_api_key: str = ""
    mt_region: str = ""          # Azure 需要，其他留空
    # 快速粗译接力：先用免密钥 google_free 出粗译（实测 0.2 秒/句），LLM 精译
    # 到了再覆盖。实时课堂里"0.2 秒看懂大意"远比"干等 2.5 秒等精译"重要；
    # 术语错误由精译兜底。关掉则回到"干等 LLM"。
    fast_translation: bool = True

    # —— 讲义工作区 ——
    vision: bool = False        # 圈选提问时是否把裁图一起发给模型（需要视觉模型）

    # —— 翻译与总结（共用一套 OpenAI 兼容接口）——
    llm_base_url: str = "http://localhost:11434/v1"
    llm_api_key: str = "ollama"
    llm_model: str = "qwen3:8b"
    translate_model: str = ""     # 留空则用 llm_model
    summary_model: str = ""
    # 推理模型思考档（low/high/max）；留空 = 不发送该参数，用接口默认档。
    # 实时翻译建议 low：实测 GLM-5.3-Flash 默认档每句先烧几百思考 token。
    llm_reasoning_effort: str = ""

    # —— 采集与断句 ——
    sample_rate: int = 16000
    silence_ms: int = 700         # 静音多久算一句话结束
    min_speech_ms: int = 350      # 短于此时长视为噪声丢弃
    max_segment_ms: int = 15000   # 一句话最长时长，到点强制切

    # —— 自动总结 ——
    auto_summary_minutes: int = 0  # 0 = 只手动

    # —— 存储与服务 ——
    data_dir: str = "~/.aula"
    host: str = "127.0.0.1"
    port: int = 8780

    def resolved_asr_model(self) -> str:
        return self.asr_model or DEFAULT_ASR_MODELS.get(self.asr_provider, "whisper-1")

    def pick_mt(self) -> str:
        """translate_engine 的归一化：llm 表示不启用专用接口。"""
        engine = (self.translate_engine or "llm").strip().lower()
        return "llm" if engine in ("", "none") else engine

    def resolved_translate_model(self) -> str:
        return self.translate_model or self.llm_model

    def resolved_summary_model(self) -> str:
        return self.summary_model or self.llm_model

    def db_path(self) -> Path:
        return Path(self.data_dir).expanduser() / "aula.db"

    def export_dir(self) -> Path:
        return Path(self.data_dir).expanduser() / "exports"

    def public(self) -> Dict[str, Any]:
        """给前端看的配置：密钥只暴露"有没有"，不暴露内容。"""
        data = asdict(self)
        data["asr_api_key"] = _mask(self.asr_api_key)
        data["mt_api_key"] = _mask(self.mt_api_key)
        data["llm_api_key"] = _mask(self.llm_api_key)
        data["languages"] = LANGUAGES
        data["asr_providers"] = ASR_PROVIDERS
        from .mt import MT_PROVIDERS
        from .providers import PROVIDERS

        data["mt_providers"] = [
            {
                "id": m.id,
                "label": m.label,
                "limits": m.limits,
                "free": m.free,
                "key": m.needs_key(),
                "key_env": m.key_env,
                "needs_region": m.needs_region,
            }
            for m in MT_PROVIDERS
        ]

        data["providers"] = [
            {
                "id": p.id,
                "label": p.label,
                "kind": p.kind,
                "base_url": p.base_url,
                "model": p.model,
                "alt_models": list(p.alt_models),
                "limits": p.limits,
                "context": p.context,
                "key": p.needs_key,
                "key_env": p.key_env,
                "key_url": p.key_url,
                "free": p.free_note,
                "asr_mode": p.asr_mode,
                "local": p.local,
            }
            for p in PROVIDERS
        ]
        data["resolved"] = {
            "asr_provider": self.pick_asr_provider(),
            "asr_model": self.resolved_asr_model(),
            "translate_model": self.resolved_translate_model(),
            "summary_model": self.resolved_summary_model(),
            "db_path": str(self.db_path()),
        }
        return data

    def pick_asr_provider(self) -> str:
        """把 auto 解析成具体实现。"""
        if self.asr_provider != "auto":
            return self.asr_provider
        if self.asr_api_key and self.asr_base_url:
            return "openai"
        if _module_available("faster_whisper"):
            return "whisper"
        return "mock"

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return secret[:4] + "*" * (len(secret) - 8) + secret[-4:]


def _module_available(name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


_FIELD_NAMES = {f.name for f in fields(Settings)}


def load(overrides: Optional[Dict[str, Any]] = None) -> Settings:
    """按优先级合并配置。未知键直接忽略，避免旧配置文件把程序打挂。"""
    data: Dict[str, Any] = {}

    target = config_path()
    if target.exists():
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                data.update({k: v for k, v in raw.items() if k in _FIELD_NAMES})
        except (json.JSONDecodeError, OSError):
            pass

    data.update(
        {
            "provider": _env("PROVIDER", data.get("provider", "")),
            "source_lang": _env("SOURCE_LANG", data.get("source_lang", "es")),
            "target_lang": _env("TARGET_LANG", data.get("target_lang", "zh")),
            "translate": _env_bool("TRANSLATE", data.get("translate", True)),
            "asr_provider": _env("ASR_PROVIDER", data.get("asr_provider", "auto")),
            "asr_model": _env("ASR_MODEL", data.get("asr_model", "")),
            "asr_base_url": _env("ASR_BASE_URL", data.get("asr_base_url", "")),
            "asr_api_key": _env("ASR_API_KEY", data.get("asr_api_key", "")),
            "llm_base_url": _env("LLM_BASE_URL", data.get("llm_base_url", "http://localhost:11434/v1")),
            "llm_api_key": _env("LLM_API_KEY", data.get("llm_api_key", "ollama")),
            "llm_model": _env("LLM_MODEL", data.get("llm_model", "qwen3:8b")),
            "vision": _env_bool("VISION", data.get("vision", False)),
            "translate_engine": _env("TRANSLATE_ENGINE", data.get("translate_engine", "llm")),
            "mt_api_key": _env("MT_API_KEY", data.get("mt_api_key", "")),
            "mt_region": _env("MT_REGION", data.get("mt_region", "")),
            "fast_translation": _env_bool("FAST_TRANSLATION", data.get("fast_translation", True)),
            "translate_model": _env("TRANSLATE_MODEL", data.get("translate_model", "")),
            "summary_model": _env("SUMMARY_MODEL", data.get("summary_model", "")),
            "llm_reasoning_effort": _env("LLM_REASONING_EFFORT", data.get("llm_reasoning_effort", "")),
            "sample_rate": _env_int("SAMPLE_RATE", data.get("sample_rate", 16000)),
            "silence_ms": _env_int("SILENCE_MS", data.get("silence_ms", 700)),
            "min_speech_ms": _env_int("MIN_SPEECH_MS", data.get("min_speech_ms", 350)),
            "max_segment_ms": _env_int("MAX_SEGMENT_MS", data.get("max_segment_ms", 15000)),
            "auto_summary_minutes": _env_int("AUTO_SUMMARY_MINUTES", data.get("auto_summary_minutes", 0)),
            "data_dir": _env("DATA_DIR", data.get("data_dir", "~/.aula")),
            "host": _env("HOST", data.get("host", "127.0.0.1")),
            "port": _env_int("PORT", data.get("port", 8780)),
        }
    )

    for key, value in (overrides or {}).items():
        if key in _FIELD_NAMES and value is not None:
            data[key] = value

    return Settings(**data)


def save(settings: Settings) -> Path:
    """写回配置文件。密钥在这里落盘，所以文件权限收紧到 0600。"""
    target = config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target
