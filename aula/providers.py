"""内置服务商预设：免费额度、限速、密钥要求，一处定义。

为什么要有这个文件：免费模型散落在十几家平台，各自的 base_url、模型名、
限速、是否需要信用卡都不一样，用户为了试一个模型要翻半天文档。这里把
"能用免费额度跑起来"的那批集中成预设，切换只需要一个 ``--provider``。

**关于数字的可信度**：下面的限速是官方文档在 2026-10 的标注值，但免费层
的额度调整很频繁（例如 Google 在 2025-12 下调过一次）。所以：

  * 数字标注为"约"，且每条都附官方文档地址
  * 真正可用的上限以各家控制台为准
  * 程序不会因为超限崩溃——遇到 429 会当作"这一句失败"继续跑
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass(frozen=True)
class Provider:
    """一个可以直接 --provider 切换的服务商预设。"""

    id: str
    label: str
    kind: str                     # chat / asr / both
    base_url: str
    model: str
    key_env: str = ""
    key_required: bool = True
    key_url: str = ""
    alt_models: Tuple[str, ...] = ()
    context: str = ""
    limits: str = ""
    free_note: str = ""
    docs: str = ""
    # 转写走哪种协议：openai = /audio/transcriptions；gemini = generateContent 内联音频
    asr_mode: str = ""
    asr_base_url: str = ""
    asr_model: str = ""
    paid: bool = False            # 有免费额度为 False，纯付费为 True
    local: bool = False
    # 推理模型思考档（low/high/max）；空 = 用接口默认。apply() 会同步到
    # settings.llm_reasoning_effort——包括"该 provider 不需要时清空"，
    # 免得从推理模型切回 Gemini 还带着一个它不认识的参数被 400。
    reasoning_effort: str = ""

    @property
    def needs_key(self) -> str:
        if self.local:
            return "不需要（本地）"
        return "需要" if self.key_required else "不需要"

    def summary(self) -> Dict[str, str]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": {"chat": "对话", "asr": "转写", "both": "对话+转写"}.get(self.kind, self.kind),
            "model": self.model,
            "context": self.context or "—",
            "limits": self.limits or "—",
            "key": self.needs_key,
            "free": self.free_note or ("付费" if self.paid else "有免费额度"),
        }


# 顺序即为 ``escucha providers`` 的展示顺序：优先放"零成本能跑通"的。
PROVIDERS: Tuple[Provider, ...] = (
    Provider(
        id="ollama",
        label="本地 Ollama",
        kind="both",
        base_url="http://localhost:11434/v1",
        model="qwen3:8b",
        key_required=False,
        local=True,
        alt_models=("qwen3:8b", "gemma3:12b", "llama3.2:3b"),
        context="取决于模型（3B≈128K，8B≈32K）",
        limits="无限制，吃本机内存/显存",
        free_note="完全免费、离线、不上传",
        docs="https://ollama.com",
    ),
    Provider(
        id="wbmanager",
        label="WorkBuddy Manager（本地网关）",
        kind="chat",
        base_url="http://127.0.0.1:7864/v1",
        model="cn:glm-5.3-flash",
        key_env="WB_MANAGER_API_KEY",
        key_url="http://127.0.0.1:7864/keys",
        alt_models=("cn:glm-5.3-flash", "cn:deepseek-v4.1-flash"),
        context="约 100 万 token",
        limits="花本机账号池的积分；模型均为推理模型，预设 low 思考档保实时性",
        free_note="复用本机 Manager 账号池，密钥在 http://127.0.0.1:7864/keys 创建；面板需保持运行",
        docs="http://127.0.0.1:7864/keys",
        reasoning_effort="low",
    ),
    Provider(
        id="deepseek",
        label="DeepSeek（官方 API）",
        kind="chat",
        base_url="https://api.deepseek.com/v1",
        model="deepseek-flash",
        key_env="DEEPSEEK_API_KEY",
        key_url="https://platform.deepseek.com/api_keys",
        alt_models=("deepseek-flash", "deepseek-v4-pro"),
        context="约 100 万 token",
        limits="按 token 计费，无长期免费层；low 档单句翻译实测 1.4–4.2 秒（中位 2.2 秒）",
        free_note="V4.1-Flash 快且支持 low/high/max 思考档，预设走 low 保实时性；"
                  "V4-Pro 更强但慢一倍（约 3.9 秒/句），不建议做逐句翻译",
        docs="https://api-docs.deepseek.com",
        reasoning_effort="low",
    ),
    Provider(
        id="gemini",
        label="Google Gemini（AI Studio）",
        kind="both",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        model="gemini-3.8-flash",
        key_env="GEMINI_API_KEY",
        key_url="https://aistudio.google.com/apikey",
        alt_models=("gemini-3.8-flash", "gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-2.5-pro"),
        context="约 100 万 token",
        limits="免费层约 10–15 RPM / 250–1500 RPD（按模型与地区调整，旧模型会下线）",
        free_note="无需信用卡；实测可直接做西语转写；高峰时段偶发 503，可换 gemini-3.5-flash",
        docs="https://ai.google.dev/gemini-api/docs/rate-limits",
        asr_mode="gemini",
        asr_base_url="https://generativelanguage.googleapis.com/v1beta",
        asr_model="gemini-3.8-flash",
    ),
    Provider(
        id="groq",
        label="Groq",
        kind="both",
        base_url="https://api.groq.com/openai/v1",
        model="llama-3.3-70b-versatile",
        key_env="GROQ_API_KEY",
        key_url="https://console.groq.com/keys",
        alt_models=("llama-3.3-70b-versatile", "llama-3.1-8b-instant", "qwen/qwen3-32b"),
        context="约 128K",
        limits="免费层按模型限速，约 30 RPM / 1000 RPD / 1.2 万 TPM",
        free_note="免费层无需信用卡，速度极快",
        docs="https://console.groq.com/docs/rate-limits",
        asr_mode="openai",
        asr_base_url="https://api.groq.com/openai/v1",
        asr_model="whisper-large-v3-turbo",
    ),
    Provider(
        id="openrouter",
        label="OpenRouter（:free 模型）",
        kind="chat",
        base_url="https://openrouter.ai/api/v1",
        model="deepseek/deepseek-chat-v3.1:free",
        key_env="OPENROUTER_API_KEY",
        key_url="https://openrouter.ai/keys",
        alt_models=(
            "deepseek/deepseek-chat-v3.1:free",
            "meta-llama/llama-3.3-70b-instruct:free",
            "google/gemma-3-27b-it:free",
        ),
        context="视模型而定，约 32K–128K",
        limits="免费模型约 20 RPM；每日 50 次（余额 <10 美元）或 1000 次",
        free_note="模型名带 :free 后缀才走免费额度",
        docs="https://openrouter.ai/docs/api-reference/limits",
    ),
    Provider(
        id="cerebras",
        label="Cerebras",
        kind="chat",
        base_url="https://api.cerebras.ai/v1",
        model="llama3.3-70b",
        key_env="CEREBRAS_API_KEY",
        key_url="https://cloud.cerebras.ai",
        alt_models=("llama3.3-70b", "llama3.1-8b", "qwen-3-32b"),
        context="约 32K–128K",
        limits="免费层约 30 RPM / 100 万 token 每天",
        free_note="推理速度最快的一档（每秒数千 token）",
        docs="https://inference-docs.cerebras.ai/support/rate-limits",
    ),
    Provider(
        id="mistral",
        label="Mistral（La Plateforme 免费层）",
        kind="both",
        base_url="https://api.mistral.ai/v1",
        model="mistral-small-latest",
        key_env="MISTRAL_API_KEY",
        key_url="https://console.mistral.ai/api-keys",
        alt_models=("mistral-small-latest", "open-mistral-nemo", "ministral-8b-latest"),
        context="约 128K",
        limits="实验层约 1 请求/秒、50 万 TPM、每月 10 亿 token",
        free_note="免费层需手机验证；数据可能用于训练",
        docs="https://docs.mistral.ai/deployment/laplateforme/tier/",
        asr_mode="openai",
        asr_base_url="https://api.mistral.ai/v1",
        asr_model="voxtral-mini-latest",
    ),
    Provider(
        id="zhipu",
        label="智谱 GLM（Flash 系列）",
        kind="chat",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        model="glm-4-flash",
        key_env="ZHIPU_API_KEY",
        key_url="https://open.bigmodel.cn/usercenter/apikeys",
        alt_models=("glm-4-flash", "glm-4.5-flash", "glm-4-air"),
        context="约 128K",
        limits="Flash 系列免费；按并发与 RPM 限速（约 5–20 并发）",
        free_note="国内直连，中文理解好；免费模型能力弱于付费档",
        docs="https://open.bigmodel.cn/dev/api",
    ),
    Provider(
        id="siliconflow",
        label="硅基流动 SiliconFlow",
        kind="both",
        base_url="https://api.siliconflow.cn/v1",
        model="Qwen/Qwen2.5-7B-Instruct",
        key_env="SILICONFLOW_API_KEY",
        key_url="https://cloud.siliconflow.cn/account/ak",
        alt_models=("Qwen/Qwen2.5-7B-Instruct", "THUDM/glm-4-9b-chat", "Qwen/Qwen3-8B"),
        context="约 32K–128K",
        limits="免费模型限速约 10–20 RPM；部分模型需实名",
        free_note="模型名不带 :free，标价 0 的即免费",
        docs="https://docs.siliconflow.cn/cn/api-reference/rate-limit",
        asr_mode="openai",
        asr_base_url="https://api.siliconflow.cn/v1",
        asr_model="FunAudioLLM/SenseVoiceSmall",
    ),
    Provider(
        id="github",
        label="GitHub Models",
        kind="chat",
        base_url="https://models.inference.ai.azure.com",
        model="gpt-4o-mini",
        key_env="GITHUB_TOKEN",
        key_url="https://github.com/settings/tokens",
        alt_models=("gpt-4o-mini", "gpt-4.1-mini", "Phi-4"),
        context="约 128K",
        limits="免费层按模型分级，低档约 15 RPM / 150 RPD",
        free_note="用 GitHub PAT 当密钥，适合已有 GitHub 账号的人",
        docs="https://docs.github.com/en/github-models/about-github-models",
    ),
    Provider(
        id="dashscope",
        label="阿里云百炼（通义千问）",
        kind="chat",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        model="qwen-plus",
        key_env="DASHSCOPE_API_KEY",
        key_url="https://bailian.console.aliyun.com",
        alt_models=("qwen-plus", "qwen-turbo", "qwen-max"),
        context="约 128K",
        limits="新用户各模型有 100 万 token 免费额度（有效期 180 天）",
        free_note="额度制而非永久免费层；用完转按量计费",
        docs="https://help.aliyun.com/zh/model-studio/models",
    ),
    Provider(
        id="openai",
        label="OpenAI（付费参考）",
        kind="both",
        base_url="https://api.openai.com/v1",
        model="gpt-4o-mini",
        key_env="OPENAI_API_KEY",
        key_url="https://platform.openai.com/api-keys",
        alt_models=("gpt-4o-mini", "gpt-4.1-mini"),
        context="约 128K",
        limits="无免费层，按 token 计费",
        free_note="作为基准参考，不推荐免费场景",
        docs="https://platform.openai.com/docs/models",
        asr_mode="openai",
        asr_base_url="https://api.openai.com/v1",
        asr_model="whisper-1",
        paid=True,
    ),
)

BY_ID: Dict[str, Provider] = {p.id: p for p in PROVIDERS}


def get(provider_id: str) -> Optional[Provider]:
    return BY_ID.get((provider_id or "").strip().lower())


def resolve_key(provider: Provider, explicit: str = "") -> str:
    """密钥优先级：显式传入 > 该服务商的约定环境变量。"""
    import os

    if explicit:
        return explicit
    if provider.key_env:
        for name in (provider.key_env, "ESCUCHA_ASR_API_KEY" if provider.kind != "chat" else ""):
            if name and os.environ.get(name):
                return os.environ[name]
    return ""


def apply(settings, explicit: Optional[set] = None):
    """把 ``provider`` 预设套用到配置上。

    ``explicit`` 里列出的字段是用户显式指定的，预设不覆盖它们——这样
    ``--provider gemini --model gemini-2.5-pro`` 这种组合才讲得通。
    """
    import dataclasses

    explicit = explicit or set()
    provider = get(getattr(settings, "provider", ""))
    if provider is None:
        return settings

    data = settings.to_dict()
    if "llm_base_url" not in explicit:
        data["llm_base_url"] = provider.base_url
    if "llm_model" not in explicit:
        data["llm_model"] = provider.model
    if "llm_api_key" not in explicit:
        key = resolve_key(provider)
        if key:
            data["llm_api_key"] = key

    if provider.asr_mode and "asr_provider" not in explicit:
        data["asr_provider"] = "gemini" if provider.asr_mode == "gemini" else "openai"
        if "asr_base_url" not in explicit:
            data["asr_base_url"] = provider.asr_base_url or provider.base_url
        if "asr_model" not in explicit:
            data["asr_model"] = provider.asr_model
        if "asr_api_key" not in explicit and data.get("llm_api_key"):
            data["asr_api_key"] = data["llm_api_key"]

    # 思考档跟随预设（含清空）：推理模型切回普通模型时，残留的
    # reasoning_effort 会让不认这个参数的接口直接 400。
    if "llm_reasoning_effort" not in explicit:
        data["llm_reasoning_effort"] = provider.reasoning_effort

    return dataclasses.replace(settings, **data)


def rows() -> List[List[str]]:
    """给 CLI 表格用的行数据。"""
    out = []
    for p in PROVIDERS:
        info = p.summary()
        out.append([info["id"], info["label"], info["kind"], info["model"], info["context"], info["limits"], info["key"]])
    return out


def render_table() -> str:
    """等宽对齐的文本表格，终端里直接看得下去。"""
    headers = ["id", "服务商", "能力", "默认模型", "上下文", "免费额度 / 限速", "密钥"]
    table = [headers] + rows()

    widths = [0] * len(headers)
    for row in table:
        for index, cell in enumerate(row):
            # 中文按两个字符宽度估算，避免错位
            width = sum(2 if ord(ch) > 0x2E80 else 1 for ch in cell)
            widths[index] = max(widths[index], width)

    def pad(text: str, width: int) -> str:
        current = sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)
        return text + " " * max(0, width - current)

    lines = [pad(headers[i], widths[i]) for i in range(len(headers))]
    lines[0] = "  ".join(pad(headers[i], widths[i]) for i in range(len(headers))).rstrip()
    separator = "  ".join("-" * widths[i] for i in range(len(headers)))
    body = ["  ".join(pad(row[i], widths[i]) for i in range(len(headers))).rstrip() for row in table[1:]]
    return "\n".join([lines[0], separator] + body)
