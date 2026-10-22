"""接口与实时链路测试。用假 LLM，不打真实模型。"""

import asyncio
import json
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from aula import config  # noqa: E402
from aula.server import create_app  # noqa: E402


def asyncio_run_with_timeout(coro, timeout: float):
    """asyncio.run 包一层超时：广播测试挂起时让测试失败而不是卡死 CI。"""
    async def runner():
        return await asyncio.wait_for(coro, timeout)
    asyncio.run(runner())

SUMMARY_PAYLOAD = {
    "overview": "本节课讲总体施工计划。",
    "points": [{"heading": "计划的作用", "detail": "汇总全部技术文件。"}],
    "terms": [{"term": "plan general", "translation": "总体计划", "note": "施工总纲"}],
    "todos": ["阅读下一节"],
    "questions": ["工期怎么拆？"],
    "emphasis": ["工期是重点"],
}


class FakeLLM:
    model = "fake"

    def __init__(self):
        self.translate_calls = 0
        self.summary_calls = 0

    def chat(self, messages, **kwargs):
        self.translate_calls += 1
        return "中文译文"

    def chat_json(self, messages, **kwargs):
        self.summary_calls += 1
        return dict(SUMMARY_PAYLOAD)


class ApiTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        # 隔离配置文件：PUT /api/config 会写盘，不能碰用户真实的 ~/.config
        self._env_backup = os.environ.get("AULA_CONFIG")
        os.environ["AULA_CONFIG"] = str(Path(self._tmp.name) / "config.json")
        self.settings = config.Settings(
            data_dir=self._tmp.name,
            asr_provider="mock",
            source_lang="es",
            target_lang="zh",
            llm_model="fake",
            auto_summary_minutes=0,
            fast_translation=False,   # 默认关：避免单测真连 google_free；接力行为有专项测试
        )
        self.app = create_app(self.settings)
        self.fake = FakeLLM()
        self.app.state.engines.client = self.fake
        self.app.state.engines.translator.client = self.fake
        self.client = TestClient(self.app)

    def tearDown(self):
        if self._env_backup is None:
            os.environ.pop("AULA_CONFIG", None)
        else:
            os.environ["AULA_CONFIG"] = self._env_backup
        self._tmp.cleanup()

    def new_session(self, title="测试课"):
        response = self.client.post("/api/sessions", json={"title": title, "course": "MF1180"})
        self.assertEqual(response.status_code, 200)
        return response.json()["id"]

    # —— 基础 ——

    def test_health(self):
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")
        self.assertEqual(body["asr"]["provider"], "mock")

    def test_index_served(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("aula", response.text)

    def test_config_roundtrip(self):
        body = self.client.get("/api/config").json()
        self.assertEqual(body["source_lang"], "es")
        self.assertIn("languages", body)
        self.assertNotIn("sk-", str(body.get("llm_api_key", "")))

        updated = self.client.put("/api/config", json={"target_lang": "en", "silence_ms": 900}).json()
        self.assertEqual(updated["target_lang"], "en")
        self.assertEqual(updated["silence_ms"], 900)

    # —— 会话 ——

    def test_session_crud(self):
        sid = self.new_session()
        listing = self.client.get("/api/sessions").json()
        self.assertEqual(len(listing), 1)

        detail = self.client.get(f"/api/sessions/{sid}").json()
        self.assertEqual(detail["session"]["title"], "测试课")
        self.assertEqual(detail["utterances"], [])

        patched = self.client.patch(f"/api/sessions/{sid}", json={"title": "改名"}).json()
        self.assertEqual(patched["title"], "改名")

        self.assertEqual(self.client.delete(f"/api/sessions/{sid}").status_code, 200)
        self.assertEqual(self.client.get(f"/api/sessions/{sid}").status_code, 404)

    def test_note_save_and_load(self):
        sid = self.new_session()
        self.client.put(f"/api/sessions/{sid}/note", json={"content": "# 笔记\n内容"})
        detail = self.client.get(f"/api/sessions/{sid}").json()
        self.assertIn("# 笔记", detail["note"])

    def test_manual_utterance(self):
        sid = self.new_session()
        response = self.client.post(
            f"/api/sessions/{sid}/utterance",
            json={"source": "Hola", "target": "你好", "start_ms": 1000, "end_ms": 2000},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["seq"], 1)

    # —— 总结与导出 ——

    def test_summary_flow(self):
        sid = self.new_session()
        self.client.post(f"/api/sessions/{sid}/utterance", json={"source": "Hola", "target": "你好"})

        empty = self.client.post(f"/api/sessions/{sid}/summary", json={})
        self.assertEqual(empty.status_code, 200)
        body = empty.json()
        self.assertEqual(body["content"]["overview"], "本节课讲总体施工计划。")
        self.assertEqual(body["covers_upto"], 1)

        latest = self.client.get(f"/api/sessions/{sid}/summary").json()
        self.assertEqual(latest["model"], "fake")

    def test_summary_requires_content(self):
        sid = self.new_session()
        response = self.client.post(f"/api/sessions/{sid}/summary", json={})
        self.assertEqual(response.status_code, 400)

    def test_summary_missing_session(self):
        self.assertEqual(self.client.post("/api/sessions/nope/summary", json={}).status_code, 404)

    def test_export_markdown(self):
        sid = self.new_session()
        self.client.post(f"/api/sessions/{sid}/utterance", json={"source": "Hola", "target": "你好"})
        self.client.put(f"/api/sessions/{sid}/note", json={"content": "我的笔记"})
        self.client.post(f"/api/sessions/{sid}/summary", json={})

        response = self.client.get(f"/api/sessions/{sid}/export")
        self.assertEqual(response.status_code, 200)
        self.assertIn("text/markdown", response.headers["content-type"])
        self.assertIn("attachment", response.headers["content-disposition"])
        self.assertIn("## 课堂总结", response.text)
        self.assertIn("我的笔记", response.text)

    # —— 实时链路 ——

    def test_websocket_transcribes_then_translates(self):
        """原文先推、译文后到：用户约 1 秒就能看到听到了什么。

        这条断言同时钉住两件事——顺序不能反（译文必须后到），以及译文
        最终要落库（否则导出与听课记录里会是空的）。
        """
        sid = self.new_session()
        pcm = b"\x00\x00" * 16000  # 1 秒静音，MockASR 不关心内容
        header = struct.pack("<II", 0, 1000)

        with self.client.websocket_connect(f"/ws/live?session_id={sid}") as ws:
            ready = ws.receive_json()
            self.assertEqual(ready["type"], "ready")
            self.assertEqual(ready["asr"]["provider"], "mock")

            ws.send_bytes(header + pcm)
            first = ws.receive_json()
            self.assertEqual(first["type"], "transcript")
            utterance = first["utterance"]
            self.assertTrue(utterance["source"])
            self.assertEqual(utterance["target"], "", "原文那条不该等译文")
            self.assertEqual(utterance["seq"], 1)

            second = ws.receive_json()
            self.assertEqual(second["type"], "translation")
            self.assertEqual(second["seq"], 1)
            self.assertEqual(second["target"], "中文译文")

        detail = self.client.get(f"/api/sessions/{sid}").json()
        self.assertEqual(len(detail["utterances"]), 1)
        self.assertEqual(detail["utterances"][0]["target"], "中文译文",
                         "译文必须回填到库里，导出与听课记录才拿得到")

    def test_websocket_rejects_unknown_session(self):
        with self.client.websocket_connect("/ws/live?session_id=nope") as ws:
            message = ws.receive_json()
            self.assertEqual(message["type"], "error")

    def test_websocket_partial_streaming_drafts(self):
        """流式草稿：说话过程中 flags=1 的帧立即转写广播，不入库。

        这是"YouTube 式边说边出字"的核心：不等整句说完（VAD 静音），
        说话中每秒就有一版中间结果。
        """
        sid = self.new_session()
        pcm = b"\x00\x00" * 16000  # 1 秒
        partial_frame = struct.pack("<III", 500, 10500, 1) + pcm
        final_frame = struct.pack("<III", 500, 11000, 0) + pcm

        with self.client.websocket_connect(f"/ws/live?session_id={sid}") as ws:
            self.assertEqual(ws.receive_json()["type"], "ready")
            ws.send_bytes(partial_frame)
            draft = ws.receive_json()
            self.assertEqual(draft["type"], "partial", "草稿帧必须立即出中间结果")
            self.assertEqual(draft["key"], 500)
            self.assertTrue(draft["source"], "草稿要有文字")

            ws.send_bytes(final_frame)
            final = ws.receive_json()
            self.assertEqual(final["type"], "transcript", "定稿走原有链路")
            self.assertEqual(final["key"], 500, "定稿带 key，前端好收掉草稿节点")

        detail = self.client.get(f"/api/sessions/{sid}").json()
        self.assertEqual(len(detail["utterances"]), 1, "草稿绝不入库，只有定稿")

    def test_websocket_fast_translation_then_refinement(self):
        """快速粗译接力：粗译随原文先到，LLM 精译随后覆盖。

        实测 LLM 精译 2.5 秒/句而 google_free 粗译 0.2 秒/句——实时课堂里
        先看到大意再等精译，比干等 2.5 秒强得多。
        """
        class FakeFastMT:
            def translate(self, text, source, target):
                return "粗译:" + text[:6]

        sid = self.new_session()
        self.app.state.engines.fast_mt = FakeFastMT()
        pcm = b"\x00\x00" * 16000
        header = struct.pack("<II", 0, 1000)

        try:
            with self.client.websocket_connect(f"/ws/live?session_id={sid}") as ws:
                self.assertEqual(ws.receive_json()["type"], "ready")
                ws.send_bytes(header + pcm)
                first = ws.receive_json()
                self.assertEqual(first["type"], "transcript")
                self.assertEqual(
                    first["utterance"]["target"],
                    "粗译:" + first["utterance"]["source"][:6],
                    "粗译必须随原文一起到",
                )
                second = ws.receive_json()
                self.assertEqual(second["type"], "translation")
                self.assertEqual(second["target"], "中文译文", "LLM 精译随后覆盖粗译")

            detail = self.client.get(f"/api/sessions/{sid}").json()
            self.assertEqual(detail["utterances"][0]["target"], "中文译文", "库里留精译")
        finally:
            self.app.state.engines.fast_mt = None

    def test_websocket_broadcasts_to_same_session(self):
        """本机助手送音频、浏览器看结果：同一节课的两个连接都要收到转写。

        广播是「系统声音·本机助手」功能的地基——助手只送音频不显示，
        浏览器不送音频只显示，没有广播这个功能就是聋子配哑巴。

        注意不能用 TestClient 测跨连接广播：它给每个 WebSocket 连接建
        独立 portal（独立事件循环），跨循环 await send 必然死锁——那是
        测试基建的假象，生产里 uvicorn 是单循环。所以这条用真服务测。
        """
        import threading
        import time as time_mod
        import urllib.request

        import uvicorn
        import websockets

        class FakeLLM:
            model = "fake"

            def chat(self, messages, **kwargs):
                return "中文译文"

            def chat_json(self, messages, **kwargs):
                return dict(SUMMARY_PAYLOAD)

        app = create_app(config.Settings(
            data_dir=self._tmp.name, asr_provider="mock", source_lang="es",
            target_lang="zh", llm_model="fake", auto_summary_minutes=0,
        ))
        fake = FakeLLM()
        app.state.engines.client = fake
        app.state.engines.translator.client = fake

        import socket as socket_mod

        with socket_mod.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        for _ in range(50):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
                break
            except Exception:
                time_mod.sleep(0.1)
        else:
            self.fail("测试服务没有起来")

        try:
            sid = self.client.post("/api/sessions", json={"title": "广播测试"}).json()["id"]
            pcm = b"\x00\x00" * 16000
            header = struct.pack("<II", 0, 1000)

            async def scenario():
                async with websockets.connect(f"ws://127.0.0.1:{port}/ws/live?session_id={sid}") as sender, \
                        websockets.connect(f"ws://127.0.0.1:{port}/ws/live?session_id={sid}") as viewer:
                    self.assertEqual(json.loads(await sender.recv())["type"], "ready")
                    self.assertEqual(json.loads(await viewer.recv())["type"], "ready")
                    await sender.send(header + pcm)
                    first = json.loads(await viewer.recv())
                    self.assertEqual(first["type"], "transcript", "浏览器必须看到助手的音频产出")
                    self.assertEqual(first["utterance"]["seq"], 1)
                    second = json.loads(await viewer.recv())
                    self.assertEqual(second["type"], "translation")
                    self.assertEqual(second["target"], "中文译文")
                    seen_by_sender = json.loads(await sender.recv())
                    self.assertEqual(seen_by_sender["type"], "transcript")

            asyncio_run_with_timeout(scenario(), 30)
        finally:
            server.should_exit = True
            thread.join(timeout=10)

    def test_websocket_broadcast_cleans_up_on_disconnect(self):
        """掉线的连接要摘出广播组：一个没关干净的旧标签页不能带崩课堂。"""
        sid = self.new_session()
        with self.client.websocket_connect(f"/ws/live?session_id={sid}") as ws:
            ws.receive_json()   # ready
            ws.close()          # 主动关：服务端收到 websocket.disconnect
        import time as time_mod
        from aula.server import _HUBS
        deadline = time_mod.time() + 3
        while time_mod.time() < deadline and sid in _HUBS:
            time_mod.sleep(0.1)
        self.assertNotIn(sid, _HUBS, "最后一个连接断开后应清空该节课的广播组")

    # —— 本机助手（ScreenCaptureKit）——

    def test_capture_start_unknown_session_is_404(self):
        response = self.client.post("/api/capture/start", json={"session_id": "nope"})
        self.assertEqual(response.status_code, 404)

    def test_capture_start_without_helper_and_toolchain_is_503(self):
        """既没有编好的助手又没有 swiftc：503 + 说清怎么办，而不是 500。"""
        from unittest import mock

        from aula import server

        sid = self.new_session()
        with mock.patch.object(server.shutil, "which", return_value=None), \
                mock.patch.object(Path, "is_file", return_value=False), \
                mock.patch.object(Path, "is_file", return_value=False):
            response = self.client.post("/api/capture/start", json={"session_id": sid})
        self.assertEqual(response.status_code, 503)
        self.assertIn("swiftc", response.json()["detail"])

    def test_capture_status_shape(self):
        response = self.client.get("/api/capture/status")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("helper_ready", body)
        self.assertIn("swiftc", body)
        self.assertEqual(body["captures"], [])

    def test_capture_stop_without_start(self):
        response = self.client.post("/api/capture/stop", json={"session_id": "whatever"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["stopped"], False)

    def test_websocket_ping(self):
        sid = self.new_session()
        with self.client.websocket_connect(f"/ws/live?session_id={sid}") as ws:
            ws.receive_json()
            ws.send_text(json.dumps({"type": "ping"}))
            self.assertEqual(ws.receive_json()["type"], "pong")


if __name__ == "__main__":
    unittest.main()
