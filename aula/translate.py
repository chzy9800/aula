"""翻译：逐句、低延迟、只出译文。

几个刻意的选择：
  * **不带上下文窗口**。给模型前后三句会让译文更顺，但每次都要多等一倍
    token，实时场景下延迟比顺滑更重要。跨句一致性靠术语表兜底。
  * **只输出译文**。任何"以下是翻译："的前缀都会污染转写面板。
  * **缓存**。课堂里"¿vale?"、"como hemos dicho" 会反复出现，命中缓存
    能省掉明显的等待。
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Optional

from .llm import LLMClient, LLMError
from .mt import BaseMT, MTError

LANG_NAMES = {
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

SYSTEM = """你是课堂同声传译。把用户给出的{src}句子翻译成{dst}。

规则：
1. 只输出译文本身，不要原文、不要解释、不要引号、不要"翻译："之类的前缀。
2. 专业术语保留准确译法，首次出现的术语在译文后用括号附上原文。
3. 数字、单位、人名、公式保持原样。
4. 句子可能是被语音识别切断的半句，照原样译，不要补全、不要润色成完整论述。
5. 如果原文只是语气词或无实义内容，输出空字符串。"""


class Translator:
    def __init__(
        self,
        client: LLMClient,
        source_lang: str = "es",
        target_lang: str = "zh",
        model: Optional[str] = None,
        cache_size: int = 512,
        mt: Optional[BaseMT] = None,
    ) -> None:
        self.client = client
        self.mt = mt
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.model = model
        self.cache: "OrderedDict[str, str]" = OrderedDict()
        self.cache_size = cache_size
        self.glossary: Dict[str, str] = {}
        self.enabled = source_lang != target_lang

    def translate(self, text: str, timeout: int = 20) -> str:
        text = (text or "").strip()
        if not text or not self.enabled:
            return text
        cached = self.cache.get(text)
        if cached is not None:
            self.cache.move_to_end(text)
            return cached

        if self.mt is not None:
            try:
                translated = _strip_noise(self.mt.translate(text, self.source_lang, self.target_lang))
            except MTError:
                # 专用接口挂了就退回大模型，别让这一句直接空着
                translated = ""
            else:
                if translated:
                    self._remember(text, translated)
                    return translated

        system = SYSTEM.format(
            src=LANG_NAMES.get(self.source_lang, self.source_lang),
            dst=LANG_NAMES.get(self.target_lang, self.target_lang),
        )
        if self.glossary:
            pairs = "；".join("%s→%s" % (k, v) for k, v in list(self.glossary.items())[:40])
            system += "\n\n固定译名（必须遵守）：" + pairs

        try:
            result = self.client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": text},
                ],
                model=self.model,
                temperature=0.1,
                max_tokens=600,
                timeout=timeout,
            )
        except LLMError:
            return ""

        result = _strip_noise(result)
        self._remember(text, result)
        return result

    def _remember(self, key: str, value: str) -> None:
        self.cache[key] = value
        if len(self.cache) > self.cache_size:
            self.cache.popitem(last=False)


def _strip_noise(text: str) -> str:
    cleaned = text.strip()
    for prefix in ("译文：", "翻译：", "中文：", "Traducción:", "Translation:"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :].strip()
    return cleaned.strip().strip('"').strip("「」").strip()
