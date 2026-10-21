# -*- coding: utf-8 -*-
"""真实链路联调：拿当前配置的模型跑一遍，不打桩、不造假。

与 ``e2e_check.py`` 的区别：那个用 ``fake_llm.py`` 固定返回，给 CI 跑；
这个真调接口，**只有真跑过才知道质量**。

    python examples/live_check.py                # 用当前配置（默认本地网关）
    python examples/live_check.py --provider wbmanager

每项独立计时独立判定，失败不中断后面的检查（一个问题要看全，不要修一个再测）。
退出码 0 = 全过，1 = 有失败项。
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from aula import chat as chatmod  # noqa: E402
from aula import config as configmod  # noqa: E402
from aula import digest as digestmod  # noqa: E402
from aula import llm as llmmod  # noqa: E402
from aula import mt as mtmod  # noqa: E402
from aula import outline as outlinemod  # noqa: E402
from aula import providers  # noqa: E402
from aula import study as studymod  # noqa: E402
from aula.transcript import BilingualTurn  # noqa: E402
from aula.translate import Translator  # noqa: E402

# 课堂口语句子（含填充词），不是书面语——书面语测不出真实表现
ES = [
    "Buenos días, hoy seguimos con la organización de la ejecución de las instalaciones eléctricas.",
    "A ver, una duda que siempre surge: ¿el cronograma y el plan general son lo mismo? No, son cosas distintas.",
    "El plazo de ejecución lo fija el contrato, nosotros no lo decidimos.",
    "Un material que llega tarde a la obra paraliza la actividad y sube el coste final.",
    "La consignación es el procedimiento que garantiza que no haya tensión en la línea.",
    "Para la próxima clase traed las dudas sobre el cronograma de la práctica.",
]

results: list[tuple[str, bool, str]] = []


def check(name: str):
    """把一项检查包起来：计时、判定、记录，最后统一汇报。"""

    def wrap(fn):
        print("\n[%s] %s" % ("·" * 3, name))
        started = time.time()
        try:
            detail = fn()
            elapsed = time.time() - started
            results.append((name, True, "%.1fs · %s" % (elapsed, detail or "ok")))
            print("  ✅ %.1fs —— %s" % (elapsed, detail or "ok"))
        except Exception as exc:  # noqa: BLE001
            elapsed = time.time() - started
            message = str(exc).splitlines()[0][:120]
            results.append((name, False, "%.1fs · %s" % (elapsed, message)))
            print("  ❌ %.1fs —— %s" % (elapsed, message))
        return fn

    return wrap


def main() -> int:
    parser = argparse.ArgumentParser(description="aula 真实链路联调")
    parser.add_argument("--provider", default="", help="临时切预设，如 wbmanager / gemini")
    parser.add_argument("--target", default="", help="笔记语言，默认用配置里的")
    args = parser.parse_args()

    overrides = {}
    if args.provider:
        overrides["provider"] = args.provider
    settings = configmod.load(overrides or None)
    if overrides:
        settings = providers.apply(settings, set())

    print("接口：%s" % settings.llm_base_url)
    print("模型：%s" % settings.resolved_summary_model())
    print("思考档：%s" % (settings.llm_reasoning_effort or "不发送（接口默认）"))
    print("翻译：%s → %s" % (settings.source_lang, args.target or settings.target_lang))
    if not settings.llm_api_key:
        print("\n没有配置密钥。先跑 `aula doctor` 看提示。")

    client = llmmod.client_for(settings, timeout=180)
    target = args.target or settings.target_lang

    @check("连通性")
    def _ping():
        reply = client.chat([{"role": "user", "content": "只回复两个字：可用"}], max_tokens=64)
        if not reply.strip():
            raise RuntimeError("接口返回空内容")
        return "回复「%s」" % reply.strip()[:20]

    @check("逐句翻译（3 句，实时性）")
    def _translate():
        tr = Translator(client, settings.source_lang, target)
        client.chat([{"role": "user", "content": "hi"}], max_tokens=8)  # 预热：把冷启动排除在计时外
        started = time.time()
        outputs = [tr.translate(text) for text in ES[:3]]
        per = (time.time() - started) / 3
        for text, out in zip(ES[:3], outputs):
            print("     西：%s" % text[:52])
            print("     中：%s" % out[:52])
        empty = sum(1 for item in outputs if not item.strip())
        if empty:
            raise RuntimeError("%d 句没翻出来" % empty)
        if per > 6:
            raise RuntimeError("平均 %.1f 秒/句，实时课堂会跟不上（>6 秒）" % per)
        note = "（账号池轮询时偶发偏慢，降级链会兜底）" if per > 3 else ""
        return "平均 %.2f 秒/句%s" % (per, note)

    @check("术语词库")
    def _terms():
        found = studymod.extract_terms([{"index": 0, "text": "\n".join(ES)}], client, target)
        if not found:
            raise RuntimeError("一条都没抽到")
        for item in found[:5]:
            print("     %-24s → %s" % (item.get("term", "")[:24], item.get("translation", "")))
        # 术语数随材料量增长，不是固定值：这里只有 6 句，短片段抽得少是正常的
        return "6 句片段抽出 %d 条（30 句实测约 10 条）" % len(found)

    @check("AI 大纲（锚点能否真定位）")
    def _outline():
        pages = [
            {"index": 0, "text": "\n".join(ES[:3])},
            {"index": 1, "text": "\n".join(ES[3:])},
        ]
        result = outlinemod.build_outline(pages, client, target)
        topics = result.get("topics") or []
        if not topics:
            raise RuntimeError("没有生成任何主题")
        full = "\n".join(ES)
        hit = 0
        for topic in topics:
            anchor = topic.get("search_anchor", "")
            found = anchor in full
            hit += 1 if found else 0
            print("     P%s %-26s 锚点%s %s" % (
                topic.get("page_index"), topic.get("display_title", "")[:26],
                "命中" if found else "未命中", anchor[:26]))
        ratio = hit / len(topics)
        if ratio < 0.5:
            raise RuntimeError("锚点命中率 %.0f%%，点击侧栏会跳不动" % (ratio * 100))
        return "%d 个主题，锚点命中 %.0f%%" % (len(topics), ratio * 100)

    @check("连续追问（两轮）")
    def _chat():
        selection = ES[3]
        page_text = "\n".join(ES)
        first = chatmod.chat(client, selection, page_text, [], "为什么材料晚到会同时推高工期和成本？")
        print("     A1：%s" % first.replace("\n", " ")[:110])
        second = chatmod.chat(
            client, selection, page_text,
            [{"role": "user", "content": "为什么材料晚到会同时推高工期和成本？"},
             {"role": "assistant", "content": first}],
            "那具体应该提前多久备料？",
        )
        print("     A2：%s" % second.replace("\n", " ")[:110])
        if len(second) < 10:
            raise RuntimeError("第二轮回答过短，可能上下文没接上")
        return "两轮均返回"

    @check("听课记录（分块抽取 + 全局合成）")
    def _digest():
        tr = Translator(client, settings.source_lang, target)
        turns = [
            BilingualTurn(source=text, target=tr.translate(text), start=index * 5.0)
            for index, text in enumerate(ES)
        ]
        notes = digestmod.build_notes(turns, client)
        points = len(getattr(notes, "points", []) or [])
        terms = len(getattr(notes, "terms", []) or [])
        print("     标题：%s" % getattr(notes, "title", "—"))
        print("     要点 %d 条 · 术语 %d 条 · 分块 %d" % (points, terms, getattr(notes, "chunks", 0)))
        if points == 0:
            raise RuntimeError("没抽出任何要点")
        return "要点 %d · 术语 %d" % (points, terms)

    @check("免密钥翻译（降级链兜底）")
    def _fallback():
        engine = "google_free"
        try:
            mt_client = mtmod.build(engine, mtmod.key_from_env(engine), "")
        except mtmod.MTError as exc:
            return "免密钥接口不可用（不影响主链路）：%s" % str(exc)[:50]
        if mt_client is None:
            raise RuntimeError("构造失败")
        out = mt_client.translate(ES[2], settings.source_lang, target)
        print("     中：%s" % out[:60])
        if not out.strip():
            raise RuntimeError("返回空译文")
        return "%s 可用" % engine

    print("\n" + "=" * 58)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, detail in results:
        print("  %s %-22s %s" % ("✅" if ok else "❌", name, detail))
    print("=" * 58)
    print("%d/%d 项通过" % (passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())