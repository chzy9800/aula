"""翻译：逐句、低延迟、只出译文。

几个刻意的选择：
  * **只带上一句，而且只在需要时带**。给模型前后三句会让译文更顺，但每次都要
    多等一倍 token，实时场景下延迟比顺滑更重要。
    为什么现在又会带上一句了：静音会落在句子中间（换气、犹豫），于是断句把
    一句话切成两半（真实 YouTube 讲座实测："...otra cosa sencilla que c" /
    "de nuestro diferencial."）。残句单独翻译会崩——"de nuestro diferencial."
    没有主语也没有语境，模型只能译成"我们的差值"。所以只在**本句看着像残句**
    （以介词/连词开头，或又短又没有句末标点）时，把上一句原样附上当参考。
    多付约 30 个输入 token，实测延迟增加不到 100ms，远小于译错的代价。
    跨句一致性仍然靠术语表兜底。
  * **只输出译文**。任何"以下是翻译："的前缀都会污染转写面板。
  * **缓存**。课堂里"¿vale?"、"como hemos dicho" 会反复出现，命中缓存
    能省掉明显的等待。
"""

from __future__ import annotations

import re
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

# 断句残句的三档信号。词表全部来自真实讲座里被切断的原句。
#
# 强信号：以介词/连词开头 —— "de nuestro diferencial."、"por tradición eléctrica."
_CONTINUATIONS_STRONG = {
    "es": (
        "de", "del", "por", "para", "con", "y", "o", "u", "que", "porque",
        "pero", "aunque", "como", "cuando", "donde", "si", "sin", "sobre",
        "entre", "hasta", "desde", "tras", "mientras", "luego", "entonces",
        "también", "además",
    ),
    "en": (
        "and", "or", "but", "that", "which", "of", "to", "for", "with", "as",
        "at", "by", "from", "while", "when", "where", "then", "than", "also",
        "because",
    ),
}

# 弱信号：冠词/代词/系动词开头时，**还要**没有句末标点才算残句。
# 只看开头会误判："El diferencial protege a las personas." 是完整句。
_CONTINUATIONS_WEAK = {
    "es": (
        "el", "la", "los", "las", "un", "una", "lo", "es", "son", "está",
        "están", "hay", "no", "ya", "se", "le", "les", "su", "sus", "tu",
        "otro", "otra", "este", "esta", "esto", "nada", "puede", "tiene",
    ),
    "en": (
        "the", "a", "an", "is", "are", "was", "were", "it", "its", "this",
        "these", "those", "we", "you", "they", "he", "she", "his", "her",
        "their", "our", "not", "no",
    ),
}

# 句子说到一半停住：结尾落在功能词上（"entramos siempre por"）
_TAIL_WORDS = frozenset((
    "de", "del", "por", "para", "con", "y", "o", "u", "que", "a", "al", "en",
    "sin", "sobre", "entre", "hasta", "desde", "the", "and", "or", "of", "to",
    "for", "with", "as", "at", "by", "from",
))

# 句末标点：没有它多半是没说完就被切了
_TERMINALS = ".!?。！？…；;"
_CONTEXT_CHARS = 240
_TRIM = ",.;:¿?¡!()[]\"'"


def _is_continuation(text: str, source_lang: str) -> bool:
    """这句是不是上一句的延续（静音切在句子中间）。"""
    stripped = (text or "").strip()
    words = [word.strip(_TRIM) for word in stripped.lower().split()]
    if not words:
        return False
    head, tail = words[0], words[-1]
    if head in _CONTINUATIONS_STRONG.get(source_lang, ()):
        return True
    if tail in _TAIL_WORDS:
        return True
    if head in _CONTINUATIONS_WEAK.get(source_lang, ()):
        return not any(stripped.endswith(mark) for mark in _TERMINALS)
    return False


SYSTEM = """你是课堂同声传译。把用户给出的{src}句子翻译成{dst}。

规则：
1. 只输出译文本身，不要原文、不要解释、不要引号、不要"翻译："之类的前缀。
2. 专业术语保留准确译法，首次出现的术语在译文后用括号附上原文。
3. 数字、单位、人名、公式保持原样。
4. 句子可能是被断句切断的半句：结合上一句判断它的意思，照原样译，不要凭空补全。
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
        # 术语表：从译文里回收，跨句保持同一术语同一译法。
        # 用 OrderedDict 是为了超量时能按"最久没用"淘汰。
        self.glossary: "OrderedDict[str, str]" = OrderedDict()
        self.glossary_limit = 60
        self.enabled = source_lang != target_lang
        # 上一句原文：只在"本句像残句"时作为上下文附上
        self._prev_source = ""

    def translate(self, text: str, timeout: int = 20) -> str:
        text = (text or "").strip()
        if not text or not self.enabled:
            return text
        cached = self.cache.get(text)
        if cached is not None:
            self.cache.move_to_end(text)
            self._prev_source = text
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
                    harvest_terms(translated, self.glossary, self.glossary_limit)
                    self._prev_source = text
                    return translated

        system = SYSTEM.format(
            src=LANG_NAMES.get(self.source_lang, self.source_lang),
            dst=LANG_NAMES.get(self.target_lang, self.target_lang),
        )
        if self.glossary:
            pairs = "；".join("%s→%s" % (k, v) for k, v in list(self.glossary.items())[:40])
            system += "\n\n固定译名（必须遵守）：" + pairs

        # 残句才带上一句：完整句子照样单独送，保持"逐句低延迟"这条底线
        with_context = bool(self._prev_source) and _is_continuation(text, self.source_lang)
        if with_context:
            user = "【上一句，仅作上下文，不要翻译】%s\n【本句原文】%s" % (
                self._prev_source[-_CONTEXT_CHARS:],
                text,
            )
        else:
            user = text

        try:
            result = self.client.chat(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                model=self.model,
                temperature=0.1,
                # 中文译文 token 数 ≈ 西语字符数的 0.6～1 倍。上限跟着句长走
                # 而不是拍死 600：短句给小上限，推理模型就没有铺开闲写的余地
                # （实测短句省 0.3 秒左右）；长句照常给足，绝不截断。
                max_tokens=min(800, 100 + len(text)),
                timeout=timeout,
            )
        except LLMError:
            # 失败也要更新上下文：这一句依然是"上一句"，下一句可能依赖它
            self._prev_source = text
            return ""

        result = _strip_noise(result)
        self._remember(text, result)
        harvest_terms(result, self.glossary, self.glossary_limit)
        self._prev_source = text
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


# 译文里回收术语：模型已被要求"首次出现的术语在译文后用括号附原文"
# （如「挂牌上锁（consignación）」），所以**不用多花一次请求**就能攒出术语表。
# 这是"同一术语在一节课里前后译法不一致"的唯一解药——单句翻译彼此不知道对方。
#
# 负向后顾 (?<!…) 是关键：它要求这个词组是紧邻括号前的**完整**中文串。不加的话
# 「这里用差动保护器（diferencial）」会被截成「动保护器」——学一个残缺译名进
# 术语表，比不学更糟（它会被当"固定译名"强制后续句子照用）。宁可跳过。
_TERM_PAIR = re.compile(
    r"(?<![一-鿿])([一-鿿]{2,5}(?:/[一-鿿]{1,5})?)\s*[（(]\s*"
    r"([A-Za-zÀ-ÿ0-9][A-Za-zÀ-ÿ0-9\s\-]{1,40})\s*[）)]"
)
# 括号里带冒号说明模型在解释而不是在标注术语，这类不学
_EXPLANATORY = re.compile(r"[：:]")


def harvest_terms(translated: str, glossary: "OrderedDict[str, str]", limit: int = 60) -> None:
    """从译文里回收「外文术语 → 中文译名」，存进 glossary 供后续句子遵守。

    只学"中文紧邻括号 + 括号里是纯外文"的组合：模型自己写成
    「价差/差额（diferencial：价差/差额）」的解释性括号会被跳过——宁可少学，
    也不要把错误的译名固化下来。

    **权衡要说明白**：锁定的是"一致"，不保证"正确"。实测 `diferencial`
    第一次可能被译成"压差"，之后整节课都会照用这个错译。之所以仍要锁：
    一节课里同一术语前后两种译法，比偶尔一个错译更难读；真要改，
    讲义页的「术语词库」是更好的来源（那里带释义，可以人工修正）。
    """
    for chinese, foreign in _TERM_PAIR.findall(translated or ""):
        if _EXPLANATORY.search(foreign):
            continue
        key = " ".join(foreign.strip().lower().split())
        if not key or len(key) > 40:
            continue
        # 「进度表/时间表」取第一个作为规范译名
        value = chinese.split("/")[0].strip()
        if not value:
            continue
        glossary.pop(key, None)
        glossary[key] = value
        while len(glossary) > limit:
            glossary.popitem(last=False)
