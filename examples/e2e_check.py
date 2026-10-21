#!/usr/bin/env python3
"""端到端检查：两个工作区一次跑完，CI 与本地都能用。

    python3 examples/fake_llm.py 8799 &
    AULA_LLM_BASE_URL=http://127.0.0.1:8799/v1 AULA_LLM_MODEL=fake \
      python3 -m aula --demo --port 8781 --data-dir /tmp/aula_e2e &
    python3 examples/e2e_check.py 8781
"""

from __future__ import annotations

import asyncio
import json
import struct
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8781
BASE = "http://127.0.0.1:%d" % PORT
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def call(path, payload=None, method="GET"):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    request = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    with OPENER.open(request, timeout=60) as response:
        return json.loads(response.read())


def text_of(path):
    with OPENER.open(BASE + path, timeout=30) as response:
        return response.read().decode()


async def live_part() -> str:
    import websockets

    session = call("/api/sessions", {"title": "端到端检查", "course": "MF1180_3"}, "POST")
    sid = session["id"]
    print("建课:", sid)

    async with websockets.connect("ws://127.0.0.1:%d/ws/live?session_id=%s" % (PORT, sid)) as ws:
        ready = json.loads(await ws.recv())
        assert ready["type"] == "ready", ready
        print("就绪: 转写=%s | %s → %s" % (
            ready["asr"]["provider"], ready["source_lang"], ready["target_lang"]
        ))
        for index in range(3):
            pcm = b"\x00\x00" * 16000
            await ws.send(struct.pack("<II", index * 4000, index * 4000 + 3000) + pcm)
            message = json.loads(await ws.recv())
            assert message["type"] == "transcript", message
            print("  第 %d 句: %s" % (message["utterance"]["seq"], message["utterance"]["source"][:40]))

    call("/api/sessions/%s/note" % sid, {"content": "## 我的笔记\n- 总体计划是第一阶段\n"}, "PUT")

    summary = call("/api/sessions/%s/summary" % sid, {"kind": "full"}, "POST")
    assert summary["content"].get("overview"), summary
    print("课上总结:", summary["content"]["overview"][:36])

    digest = call("/api/sessions/%s/digest" % sid, {}, "POST")
    markdown = digest["markdown"]
    for heading in ("## 核心要点", "## 关键术语", "## 待自查问题", "## 我的随手记"):
        assert heading in markdown, "缺小节：" + heading
    print("听课记录: %d 字符，%d 个要点，小节齐全" % (len(markdown), len(digest["notes"]["points"])))
    open("/tmp/aula_digest.md", "w", encoding="utf-8").write(markdown)

    return sid


def reader_part() -> None:
    outline = call(
        "/api/reader/outline",
        {
            "pages": [
                {"index": 1, "text": "UF2 Organizacion de la ejecucion\n\nEl plan general de construccion recoge toda la documentacion tecnica."},
                {"index": 2, "text": "El plazo y el cronograma\n\nLa prevencion de riesgos laborales es fundamental."},
            ],
            "target_lang": "zh",
        },
        "POST",
    )
    topics = outline["topics"]
    assert topics, outline
    for topic in topics:
        for field in ("display_title", "search_anchor", "page_index", "difficulty_score"):
            assert field in topic, topic
    assert all(t["page_index"] in (1, 2) for t in topics), topics
    print("讲义大纲: %d 个主题，锚点与页码齐全（%s）" % (len(topics), "缓存" if outline.get("cached") else "新算"))

    body = call(
        "/api/reader/translate",
        {"blocks": ["El plazo es de cinco semanas.", "Falta material."], "source": "es", "target": "zh", "engine": "llm"},
        "POST",
    )
    assert body["translations"] and all(body["translations"]), body
    print("讲义翻译: %s → %s" % (body["engine"], body["translations"][0][:20]))

    terms = call(
        "/api/reader/terms",
        {"pages": [{"index": 1, "text": "El plan general de construccion y el cronograma."}], "target_lang": "zh"},
        "POST",
    )["terms"]
    assert terms and all(t.get("term") and t.get("translation") for t in terms), terms
    print("术语抽取: %d 个（%s）" % (len(terms), terms[0]["term"]))

    card = call(
        "/api/reader/lecturer-card",
        {"selection_text": "El plazo y el cronograma", "page_text": "…", "question": "看不懂怎么排"},
        "POST",
    )["card"]
    assert card["lecturer_question"], card
    print("提问卡: %s" % card["lecturer_question"][:40])

    answer = call(
        "/api/reader/ask",
        {"question": "这句什么意思？", "selection_text": "plan general", "page_text": "…"},
        "POST",
    )
    assert answer["answer"], answer
    print("圈选提问:", answer["answer"][:30].replace("\n", " "))

    sample = urllib.request.Request(BASE + "/api/sample")
    with OPENER.open(sample, timeout=30) as response:
        head = response.read(4)
    assert head == b"%PDF", head
    print("示例讲义: PDF 可下载")


async def main() -> int:
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("需要 websockets：pip install 'uvicorn[standard]'", file=sys.stderr)
        return 2

    health = call("/api/health")
    print("健康:", health["status"], "| 翻译", health["translate"]["engine"], "| 讲义", health["reader"]["engine"])

    sid = await live_part()
    reader_part()

    exported = text_of("/api/sessions/%s/export" % sid)
    assert "## 转写全文" in exported
    print("导出: %d 字符，结构完整" % len(exported))
    print("\n全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
