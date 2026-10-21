#!/usr/bin/env python3
"""假的 OpenAI 兼容服务：不花一分钱验证整条链路。

    python3 examples/fake_llm.py 8799
    AULA_LLM_BASE_URL=http://127.0.0.1:8799/v1 AULA_LLM_MODEL=fake python3 -m aula --demo

按 system 提示词判断该返回什么：

    课堂速记员（分块抽取）→ 片段 JSON
    整理成复习资料（合成）→ 完整听课记录 JSON
    课堂记录整理助手（课上总结）→ 总结 JSON
    讲义翻译 → 编号译文
    其余（圈选提问）→ 一段导师式回答

它不是模型，只用来固定输出结构、跑 CI 和截图；真实质量必须接真模型看。
"""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Dict, List

CHUNK = {
    "points": [
        "总体施工计划（plan general de construcción）把全部阶段、人力和材料一次写清",
        "缺料会让活动停摆，工期与成本同时上升",
    ],
    "terms": [{"term": "plan general de construcción", "translation": "总体施工计划", "note": "施工组织的总纲"}],
    "emphasis": ["工期控制是考点"],
    "assignments": ["阅读 UD4 的材料"],
    "questions": ["五周工期怎么拆分到每道工序？"],
}

SYNTH = {
    "title": "电气装置执行阶段的组织",
    "summary": "这节课讲施工组织：先有总体计划，再排工期与人力，最后用控制文件验收。计划的目的是让材料、人力和工期三者不互相拖累。",
    "points": [
        {"heading": "总体计划的作用", "detail": "把全部阶段、人力与材料写进同一份文件，后续每一步都以它为基准核对。"},
        {"heading": "工期与进度表", "detail": "工期由合同固定，进度表把活动排到具体日期；材料晚到会同时推高工期和成本。"},
        {"heading": "控制与验收", "detail": "控制文件规定如何检查进度与质量，收尾做功能与绝缘测试并记录结果。"},
    ],
    "terms": [
        {"term": "plan general de construcción", "translation": "总体施工计划", "note": "施工组织的总纲"},
        {"term": "cronograma", "translation": "进度表", "note": "活动与日期的对应表"},
        {"term": "plazo", "translation": "工期", "note": "合同约定的完成时限"},
    ],
    "emphasis": ["工期控制与风险预防贯穿全程，是常见考点"],
    "assignments": ["阅读 UD4 的材料并整理进度表"],
    "questions": ["五周工期如何拆分到每道工序？", "材料延误时进度表该怎么调整？"],
}

SUMMARY = {
    "overview": "本节课讲电气装置执行阶段的组织：总体计划、工期与进度表、控制与验收。",
    "points": ["总体计划覆盖全部阶段与资源", "缺口材料会推高工期与成本", "控制文件规定验收方式"],
    "terms": [{"term": "cronograma", "translation": "进度表", "note": "活动与日期的对应表"}],
    "emphasis": ["工期控制是考点"],
    "todos": ["整理一份两周进度表"],
    "questions": ["风险预防如何贯穿全过程？"],
}

ANSWER = (
    "这句话在说总体施工计划的作用：把整个安装过程的所有阶段、人力和材料一次写清楚，"
    "后面的每一步都拿它当基准。\n\n"
    "为什么重要：如果没有这份计划，材料到得晚或人手不够时工程只能停，工期和成本同时上升。\n\n"
    "下一步可以看讲义第 3 节，那里把计划拆成具体文件。"
)


def pick_reply(system: str, user: str) -> str:
    if "课堂速记员" in system:
        return json.dumps(CHUNK, ensure_ascii=False)
    if "整理成复习资料" in system:
        return json.dumps(SYNTH, ensure_ascii=False)
    if "课堂记录整理助手" in system:
        return json.dumps(SUMMARY, ensure_ascii=False)
    if "同声传译" in system:
        return _fake_sentence(user)
    if "讲义翻译" in system:
        # 用户按 "1. 原文" 逐行给，逐行回译，保持一一对应
        items = [re.sub(r"^\s*\d+\.\s*", "", line).strip() for line in user.split("\n") if line.strip()]
        return "\n".join("%d. %s" % (index, _fake_sentence(text)) for index, text in enumerate(items, 1))
    return ANSWER


SENTENCE_MAP = [
    ("organización de las etapas", "我们来看执行阶段的组织。"),
    ("plan general de construcción", "总体施工计划汇总了全部技术文件。"),
    ("croquis", "先绘制草图，然后绘制细部图纸。"),
    ("prevención de riesgos", "职业风险的预防贯穿整个过程。"),
    ("plazo", "工期是合同里约定的时间。"),
    ("asignar los recursos humanos", "需要分配人力和每项活动的任务。"),
    ("acopio de materiales", "备料可以避免工程中途停工。"),
    ("organización de la ejecución", "电气装置执行阶段的组织。"),
    ("documentos que componen", "计划的组成文件：说明、图纸与人力分配。"),
    ("descripción detallada", "装置的详细说明，含每个回路与受电设备。"),
    ("control y pruebas", "收尾做功能测试与绝缘测试，并记录结果。"),
    ("ciclo formativo", "职业培训课程 · MF1180_3 模块 · 课堂讲义。"),
    ("instalación de una vivienda", "以一户住宅的电气安装为例。"),
]


def _fake_sentence(text: str) -> str:
    lowered = text.lower()
    for key, value in SENTENCE_MAP:
        if key in lowered:
            return value
    return "（示例）这一段继续讲解施工计划的执行。"


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload: Dict[str, Any] = json.loads(self.rfile.read(length) or b"{}")
        messages: List[Dict[str, Any]] = payload.get("messages") or []
        system = " ".join(str(item.get("content")) for item in messages if item.get("role") == "system")
        user = ""
        if messages:
            raw = messages[-1].get("content")
            if isinstance(raw, list):
                user = " ".join(str(part.get("text", "")) for part in raw if isinstance(part, dict))
            else:
                user = str(raw or "")
        reply = pick_reply(system, user)
        body = json.dumps(
            {
                "id": "fake",
                "object": "chat.completion",
                "model": payload.get("model", "fake"),
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": reply}, "finish_reason": "stop"}
                ],
            },
            ensure_ascii=False,
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: Any) -> None:
        return


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
    server = HTTPServer(("127.0.0.1", port), Handler)
    print("aula fake LLM: http://127.0.0.1:%d/v1" % port, flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
