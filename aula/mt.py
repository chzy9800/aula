"""专用翻译接口（机器翻译 API），与"用大模型翻译"并列的一条路。

为什么要有它：LLM 翻译质量好、能处理术语和上下文，但一次请求 0.6–2 秒，
高峰期还会 503；专用 MT 接口通常 100–300ms 返回，免费额度按月给字符数
（Azure 每月 200 万字符），课堂逐句翻译的延迟更稳。

代价也明确：MT 接口是逐句直译，不会像 LLM 那样把术语原文注在括号里，
遇到断句（半句话）也更容易译歪。所以默认仍然是 LLM，MT 作为可选引擎。

支持：Google Cloud Translation v2、Azure Translator、DeepL（免费版）。
"""

from __future__ import annotations

import html
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .net import opener_for


class MTError(RuntimeError):
    pass


@dataclass(frozen=True)
class MTProvider:
    id: str
    label: str
    endpoint: str
    key_env: str
    key_url: str
    limits: str
    free: str
    docs: str
    needs_region: bool = False
    paid: bool = False
    key_required: bool = True

    def needs_key(self) -> str:
        return "需要" if self.key_required else "不需要（免密钥）"


MT_PROVIDERS: Tuple[MTProvider, ...] = (
    MTProvider(
        id="google",
        label="Google Cloud Translation v2",
        endpoint="https://translation.googleapis.com/language/translate/v2",
        key_env="GOOGLE_TRANSLATE_API_KEY",
        key_url="https://console.cloud.google.com/apis/credentials",
        limits="每月前 50 万字符免费，超出约 $20/百万字符",
        free="需要 GCP 项目并开通结算（绑卡，但免费额度内不扣费）",
        docs="https://cloud.google.com/translate/pricing",
    ),
    MTProvider(
        id="azure",
        label="Azure AI Translator",
        endpoint="https://api.cognitive.microsofttranslator.com/translate",
        key_env="AZURE_TRANSLATOR_KEY",
        key_url="https://portal.azure.com/#create/Microsoft.CognitiveServicesTextTranslation",
        limits="F0 免费层：每月 200 万字符，每秒 1 次请求",
        free="免费层不需要付费，但要 Azure 订阅（注册需卡做验证）",
        docs="https://learn.microsoft.com/azure/ai-services/translator/service-limits",
        needs_region=True,
    ),
    MTProvider(
        id="google_free",
        label="Google 翻译（免密钥）",
        endpoint="https://clients5.google.com/translate_a/t",
        key_env="",
        key_url="https://translate.google.com",
        limits="无官方额度说明；按 IP 限流，密集请求会被拒",
        free="零配置：不用注册、不用密钥、不用绑卡",
        docs="https://translate.google.com",
        key_required=False,
    ),
    MTProvider(
        id="mymemory",
        label="MyMemory（免密钥）",
        endpoint="https://api.mymemory.translated.net/get",
        key_env="",
        key_url="https://mymemory.translated.net",
        limits="匿名每天约 1000 词；填邮箱可到 10000 词",
        free="零配置，质量一般（社区记忆库）",
        docs="https://mymemory.translated.net/doc/spec.php",
        key_required=False,
    ),
    MTProvider(
        id="libre",
        label="LibreTranslate（公共实例）",
        endpoint="https://translate.disroot.org",
        key_env="",
        key_url="https://libretranslate.com",
        limits="公共实例共享额度，慢且可能排队；可自建无限量",
        free="零配置；质量弱于 Google，但完全开源可自托管",
        docs="https://libretranslate.com/docs",
        key_required=False,
    ),
    MTProvider(
        id="deepl",
        label="DeepL API Free",
        endpoint="https://api-free.deepl.com/v2/translate",
        key_env="DEEPL_API_KEY",
        key_url="https://www.deepl.com/pro-api",
        limits="每月 50 万字符免费，单次请求上限 50 个文本",
        free="免费版 endpoint 是 api-free.deepl.com，注册需卡验证",
        docs="https://developers.deepl.com/docs/getting-started/your-first-translation",
    ),
)

MT_BY_ID: Dict[str, MTProvider] = {p.id: p for p in MT_PROVIDERS}

# 各家的语言代码不统一，集中映射一次，避免调用点到处写 if
_GOOGLE = {"zh": "zh-CN", "es": "es", "en": "en", "ja": "ja", "ko": "ko", "fr": "fr", "de": "de", "it": "it", "pt": "pt", "ru": "ru", "ar": "ar"}
_AZURE = {"zh": "zh-Hans", "es": "es", "en": "en", "ja": "ja", "ko": "ko", "fr": "fr", "de": "de", "it": "it", "pt": "pt", "ru": "ru", "ar": "ar"}
_DEEPL_SRC = {"es": "ES", "en": "EN", "zh": "ZH", "fr": "FR", "de": "DE", "it": "IT", "pt": "PT", "ja": "JA", "ko": "KO", "ru": "RU"}
_DEEPL_DST = {"zh": "ZH-HANS", "es": "ES", "en": "EN-US", "ja": "JA", "ko": "KO", "fr": "FR", "de": "DE", "it": "IT", "pt": "PT-BR", "ru": "RU"}


def get(mt_id: str) -> Optional[MTProvider]:
    return MT_BY_ID.get((mt_id or "").strip().lower())


class BaseMT:
    name = "base"

    def translate(self, text: str, source: str, target: str) -> str:
        raise NotImplementedError

    def describe(self) -> Dict[str, str]:
        return {"engine": self.name}


_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0 Safari/537.36"
)
_LIBRE = {"zh": "zh", "es": "es", "en": "en", "ja": "ja", "ko": "ko", "fr": "fr", "de": "de", "it": "it", "pt": "pt", "ru": "ru", "ar": "ar"}


class GoogleFreeMT(BaseMT):
    """Google 翻译的网页接口，无需任何密钥。

    2026-10-22 逐个实测（同一台机器、同一个 UA）：

    ============================================  ======  ==========================
    端点                                         结果    说明
    ============================================  ======  ==========================
    translate.google.com/?op=translate            302     网页不是接口，body 为空
    translate.google.com/translate_a/single       429     返回 "Sorry..." 拦截页
    translate.google.com/m                        302     要 cookie/同意页
    clients5.google.com/translate_a/t             200     扁平数组 ["译文"]，在用
    clients5.google.com/translate_a/single        200     嵌套数组，同主机备用
    clients5.google.com + client=gtx/webapp        403/拦  客户号也不通用
    ============================================  ======  ==========================

    所以顺序是：先 clients5 的 /t，再同主机的 /single，最后才试已废弃的老主机
    （换网络也许能通，留着不亏）。前两个都返回可解析 JSON 才算成功。

    注意：这是 Google 未公开的接口，不属于任何 SLA。密集请求会被按 IP
    限流，商业用途请改用官方 Cloud Translation。
    """

    name = "google_free"

    DEFAULT_BASE = "https://clients5.google.com/translate_a"

    def __init__(self, timeout: int = 15, base: str = "") -> None:
        self.timeout = timeout
        base = (base or self.DEFAULT_BASE).rstrip("/")
        # 也接受旧的完整端点写法（.../translate_a/t），归一成 base 再派生路径
        self.base = base[:-2] if base.endswith("/t") else base

    # 按实测可用性排序：同主机两条路径都通，老主机已废弃但留作最后希望
    PATHS = (
        ("{base}/t", {"client": "dict-chrome-ex"}),
        ("{base}/single", {"client": "dict-chrome-ex", "dt": "t"}),
        ("https://translate.googleapis.com/translate_a/single", {"client": "gtx", "dt": "t"}),
    )

    def translate(self, text: str, source: str, target: str) -> str:
        src = _GOOGLE.get(source, source)
        dst = _GOOGLE.get(target, target)

        for template, extra in self.PATHS:
            query = urllib.parse.urlencode({"sl": src, "tl": dst, "q": text, **extra})
            url = template.format(base=self.base) + "?" + query
            parsed = self._parse(_get(url, self.timeout, self.name))
            if parsed:
                return parsed
        raise MTError("Google 免密钥接口没有返回译文（可能被限流或页面改版）")

    @staticmethod
    def _parse(raw: str) -> str:
        payload = raw.strip()
        if not payload or payload.startswith("<"):
            return ""
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            return ""
        if not isinstance(data, list) or not data:
            return ""
        # ["译文"] 或 ["译文","原文"]
        if isinstance(data[0], str):
            return data[0].strip()
        # [[["译文","原文",...], ...], ...]
        if isinstance(data[0], list) and data[0] and isinstance(data[0][0], list):
            return "".join(str(part[0]) for part in data[0] if isinstance(part, list) and part).strip()
        if isinstance(data[0], list) and data[0] and isinstance(data[0][0], str):
            return data[0][0].strip()
        return ""

    def describe(self) -> Dict[str, str]:
        return {"engine": self.name, "key": "免密钥"}


class MyMemoryMT(BaseMT):
    """MyMemory：免注册，匿名约 1000 词/天。

    质量明显弱于 Google（走的是翻译记忆库），但胜在稳定、有明确配额说明。
    """

    name = "mymemory"

    def __init__(self, timeout: int = 15, email: str = "") -> None:
        self.timeout = timeout
        self.email = email

    def translate(self, text: str, source: str, target: str) -> str:
        params = {
            "q": text,
            "langpair": "%s|%s" % (_GOOGLE.get(source, source), _GOOGLE.get(target, target)),
        }
        if self.email:
            params["de"] = self.email
        raw = _get(MT_BY_ID["mymemory"].endpoint + "?" + urllib.parse.urlencode(params), self.timeout, self.name)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise MTError("MyMemory 响应不是 JSON：%s" % raw[:150]) from exc
        if data.get("quotaFinished"):
            raise MTError("MyMemory 今日额度已用完（匿名约 1000 词/天）")
        status = data.get("responseStatus")
        if status not in (200, "200", None):
            raise MTError("MyMemory 返回 %s：%s" % (status, data.get("responseDetails")))
        text_out = ((data.get("responseData") or {}).get("translatedText") or "").strip()
        if not text_out:
            raise MTError("MyMemory 没有返回译文")
        return html.unescape(text_out)

    def describe(self) -> Dict[str, str]:
        return {"engine": self.name, "key": "免密钥", "quota": "约 1000 词/天"}


class LibreTranslateMT(BaseMT):
    """LibreTranslate 公共实例（默认 disroot）。开源、可自建、免密钥。"""

    name = "libre"

    def __init__(self, host: str = "", timeout: int = 20) -> None:
        self.host = (host or MT_BY_ID["libre"].endpoint).rstrip("/")
        self.timeout = timeout

    def translate(self, text: str, source: str, target: str) -> str:
        payload = {
            "q": text,
            "source": _LIBRE.get(source, source),
            "target": _LIBRE.get(target, target),
            "format": "text",
        }
        data = _post_json(self.host + "/translate", payload, {}, self.timeout, self.name)
        if isinstance(data, dict) and data.get("translatedText"):
            return str(data["translatedText"]).strip()
        raise MTError("LibreTranslate 响应异常：%s" % str(data)[:200])

    def describe(self) -> Dict[str, str]:
        return {"engine": self.name, "host": self.host, "key": "免密钥"}


def _get(url: str, timeout: int, name: str) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": "*/*"}, method="GET")
    return _open(request, timeout, name)



class GoogleTranslateMT(BaseMT):
    name = "google"

    def __init__(self, api_key: str, timeout: int = 15) -> None:
        if not api_key:
            raise MTError("Google 翻译需要密钥：GOOGLE_TRANSLATE_API_KEY")
        self.api_key = api_key
        self.endpoint = MT_BY_ID["google"].endpoint
        self.timeout = timeout

    def translate(self, text: str, source: str, target: str) -> str:
        payload = {
            "q": [text],
            "source": _GOOGLE.get(source, source),
            "target": _GOOGLE.get(target, target),
            "format": "text",
        }
        url = "%s?key=%s" % (self.endpoint, self.api_key)
        data = _post_json(url, payload, {}, self.timeout, self.name)
        try:
            return html.unescape(data["data"]["translations"][0]["translatedText"])
        except (KeyError, IndexError, TypeError) as exc:
            raise MTError("Google 翻译响应异常：%s" % str(data)[:200]) from exc

    def describe(self) -> Dict[str, str]:
        return {"engine": self.name, "free": "50 万字符/月"}


class AzureTranslatorMT(BaseMT):
    name = "azure"

    def __init__(self, api_key: str, region: str = "", timeout: int = 15) -> None:
        if not api_key:
            raise MTError("Azure 翻译需要密钥：AZURE_TRANSLATOR_KEY")
        self.api_key = api_key
        self.region = region
        self.endpoint = MT_BY_ID["azure"].endpoint
        self.timeout = timeout

    def translate(self, text: str, source: str, target: str) -> str:
        query = urllib.parse.urlencode(
            {"api-version": "3.0", "from": _AZURE.get(source, source), "to": _AZURE.get(target, target)}
        )
        headers = {
            "Ocp-Apim-Subscription-Key": self.api_key,
            "Content-Type": "application/json",
        }
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        data = _post_json(self.endpoint + "?" + query, [{"Text": text}], headers, self.timeout, self.name)
        try:
            return data[0]["translations"][0]["text"]
        except (KeyError, IndexError, TypeError) as exc:
            raise MTError("Azure 翻译响应异常：%s" % str(data)[:200]) from exc

    def describe(self) -> Dict[str, str]:
        return {"engine": self.name, "region": self.region or "global", "free": "200 万字符/月"}


class DeepLMT(BaseMT):
    name = "deepl"

    def __init__(self, api_key: str, timeout: int = 15) -> None:
        if not api_key:
            raise MTError("DeepL 需要密钥：DEEPL_API_KEY")
        self.api_key = api_key
        self.endpoint = MT_BY_ID["deepl"].endpoint
        self.timeout = timeout

    def translate(self, text: str, source: str, target: str) -> str:
        body = urllib.parse.urlencode(
            {
                "text": text,
                "source_lang": _DEEPL_SRC.get(source, source.upper()),
                "target_lang": _DEEPL_DST.get(target, target.upper()),
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            self.endpoint,
            data=body,
            headers={
                "Authorization": "DeepL-Auth-Key %s" % self.api_key,
                "Content-Type": "application/x-www-form-urlencoded",
            },
            method="POST",
        )
        raw = _open(request, self.timeout, self.name)
        try:
            return json.loads(raw)["translations"][0]["text"]
        except (KeyError, IndexError, json.JSONDecodeError) as exc:
            raise MTError("DeepL 响应异常：%s" % raw[:200]) from exc

    def describe(self) -> Dict[str, str]:
        return {"engine": self.name, "free": "50 万字符/月"}


def _open(request: urllib.request.Request, timeout: int, name: str) -> str:
    try:
        with opener_for(request.full_url).open(request, timeout=timeout) as response:
            return response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:300]
        hint = ""
        if exc.code == 401 or exc.code == 403:
            hint = "（密钥无效或无权访问）"
        elif exc.code == 429:
            hint = "（超出免费额度或请求过快）"
        elif exc.code == 456:
            hint = "（DeepL 本月额度用尽）"
        raise MTError("%s 返回 %d%s：%s" % (name, exc.code, hint, detail)) from exc
    except urllib.error.URLError as exc:
        raise MTError("无法连接 %s：%s" % (name, exc.reason)) from exc


def _post_json(url: str, payload, headers: Dict[str, str], timeout: int, name: str) -> object:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers=dict({"Content-Type": "application/json"}, **headers),
        method="POST",
    )
    raw = _open(request, timeout, name)
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise MTError("%s 响应不是 JSON：%s" % (name, raw[:150])) from exc


def key_from_env(engine: str) -> str:
    """密钥优先用显式传入的；没传就读该引擎约定的环境变量。"""
    import os

    provider = get(engine)
    if provider is None:
        return ""
    return os.environ.get(provider.key_env, "") or os.environ.get("ESCUCHA_MT_API_KEY", "")


def build(engine: str, api_key: str, region: str = "") -> Optional[BaseMT]:
    """按引擎名构造 MT 客户端；engine 为 llm 或空时返回 None（表示走大模型）。"""
    engine = (engine or "llm").strip().lower()
    if engine in ("llm", "", "none"):
        return None
    provider = get(engine)
    if provider is None:
        raise MTError("未知翻译引擎：%s" % engine)
    if provider.key_required:
        api_key = api_key or key_from_env(engine)
    if engine == "google_free":
        return GoogleFreeMT()
    if engine == "mymemory":
        return MyMemoryMT()
    if engine == "libre":
        return LibreTranslateMT()
    if engine == "google":
        return GoogleTranslateMT(api_key)
    if engine == "azure":
        return AzureTranslatorMT(api_key, region)
    if engine == "deepl":
        return DeepLMT(api_key)
    raise MTError("未知翻译引擎：%s（可选 llm / google / azure / deepl）" % engine)


def rows() -> List[List[str]]:
    return [
        [
            p.id,
            p.label,
            p.limits,
            p.needs_key(),
            (p.key_env + (" + 区域" if p.needs_region else "")) if p.key_required else "—",
        ]
        for p in MT_PROVIDERS
    ]


def render_table() -> str:
    headers = ["id", "翻译接口", "额度 / 限制", "密钥", "密钥环境变量"]
    table = [headers] + rows()

    def width(text: str) -> int:
        return sum(2 if ord(ch) > 0x2E80 else 1 for ch in text)

    def pad(text: str, size: int) -> str:
        return text + " " * max(0, size - width(text))

    sizes = [max(width(row[i]) for row in table) for i in range(len(headers))]
    lines = ["  ".join(pad(headers[i], sizes[i]) for i in range(len(headers))).rstrip()]
    lines.append("  ".join("-" * sizes[i] for i in range(len(headers))))
    for row in table[1:]:
        lines.append("  ".join(pad(row[i], sizes[i]) for i in range(len(headers))).rstrip())
    return "\n".join(lines)
