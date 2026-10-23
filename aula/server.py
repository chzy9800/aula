"""HTTP 与 WebSocket 服务。

实时链路的协议（刻意做得很薄，方便你自己写客户端）：

    客户端 → 服务端
      文本帧  {"type": "ping"}                       保活
      文本帧  {"type": "flush"}                      强制把队列里剩下的处理完
      二进制帧 前 8 字节 = start_ms、end_ms（各 uint32 小端）+ 之后是 PCM16

    服务端 → 客户端
      {"type": "ready", ...}                         连接就绪，带上生效的模型
      {"type": "transcript", ...}                    一句话的转写 + 译文
      {"type": "partial", ...}                       流式草稿（边说边出字，不入库）
      {"type": "summary", ...}                       自动总结推送
      {"type": "status", ...}                        队列状态
      {"type": "error", "message": "..."}             出错但不掉线

为什么用二进制帧而不是 base64：一段 5 秒 16kHz PCM 是 160KB，base64 后
膨胀 33%，而且浏览器还要多做一次编解码。二进制直接扔过去。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from fastapi import Body, FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config, digest, doc_render, mt as mtmod, providers, reader, store
from .asr import ASRError, TimedASR, build_asr
from .llm import LLMClient, LLMError, client_for
from .summarize import summarize
from .transcript import BilingualTurn
from .translate import Translator

STATIC_DIR = Path(__file__).parent / "static"
HELPER_SOURCE = Path(__file__).parent / "helpers" / "main.swift"

logger = logging.getLogger("aula.server")

# —— 会话广播：同一节课的连接共享转写结果 ——
#
# 为什么要广播：本机助手（ScreenCaptureKit）也是一个 WS 客户端，它只负责
# 送音频；转写结果必须同时到达浏览器，用户才能看见。广播之后顺带解锁了
# 「多屏同看」——两个标签页开着同一节课，看到的内容完全一致。
_HUBS: Dict[str, Set[WebSocket]] = {}
# 本机助手子进程，按 session 记账
_CAPTURES: Dict[str, subprocess.Popen] = {}


class Engines:
    """把模型实例集中管理，配置变更时整体重建。"""

    def __init__(self, settings: config.Settings) -> None:
        self._lock = asyncio.Lock()
        # 本地 whisper 模型不支持并发推理：草稿转写与定稿转写共享这把锁，
        # 谁先拿到谁先跑，另一个等——乱并发会崩解释器级的状态。
        self.asr_lock = asyncio.Lock()
        self.settings = settings
        self.rebuild(settings)

    def rebuild(self, settings: config.Settings) -> None:
        self.settings = settings
        self.client = client_for(settings, model=settings.llm_model)
        # 专用翻译接口（Google/Azure/DeepL）与 LLM 翻译二选一；构造失败不炸整机，
        # 退回 LLM 并让 /api/health 暴露原因。
        self.mt_error = ""
        mt_client = None
        engine = settings.pick_mt()
        if engine != "llm":
            try:
                mt_client = mtmod.build(
                    engine,
                    settings.mt_api_key or mtmod.key_from_env(engine),
                    settings.mt_region,
                )
            except mtmod.MTError as exc:
                self.mt_error = str(exc)
        self.translator = Translator(
            client=self.client,
            source_lang=settings.source_lang,
            target_lang=settings.target_lang,
            model=settings.resolved_translate_model(),
            mt=mt_client,
        )
        # 快速粗译引擎（google_free，实测 0.2 秒/句）：原文一出立刻给个能懂的
        # 中文，LLM 精译随后覆盖。构造失败不炸——只是没有快速档。
        self.fast_mt = None
        if settings.fast_translation and self.translator.enabled and mt_client is None:
            try:
                self.fast_mt = mtmod.build("google_free", mtmod.key_from_env("google_free"), "")
            except mtmod.MTError as exc:
                logger.info("快速粗译不可用，按原样等精译：%s", exc)
        self.asr = TimedASR(build_asr(settings))

    async def update(self, settings: config.Settings) -> None:
        async with self._lock:
            self.rebuild(settings)


def create_app(settings: Optional[config.Settings] = None) -> FastAPI:
    settings = settings or config.load()
    Path(settings.data_dir).expanduser().mkdir(parents=True, exist_ok=True)
    store.init_db(settings.db_path())

    app = FastAPI(title="aula", version=__version__)
    engines = Engines(settings)
    app.state.engines = engines

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.include_router(reader.create_router(settings))

    # —— 基础信息 ——

    @app.get("/api/health")
    async def health() -> Dict[str, Any]:
        current = engines.settings
        return {
            "status": "ok",
            "version": __version__,
            "asr": engines.asr.describe(),
            "translate": {
                "enabled": engines.translator.enabled,
                "source_lang": current.source_lang,
                "target_lang": current.target_lang,
                "engine": current.pick_mt(),
                "model": current.resolved_translate_model(),
                "mt": engines.translator.mt.describe() if engines.translator.mt else None,
                "mt_error": engines.mt_error,
            },
            "llm": {"base_url": current.llm_base_url, "model": current.llm_model},
            "reader": {
                "engine": current.pick_mt(),
                "llm_model": current.llm_model,
                "vision": current.vision,
            },
            "digest": {"model": current.resolved_summary_model()},
            "db": str(current.db_path()),
        }

    @app.get("/api/config")
    async def get_config() -> Dict[str, Any]:
        return engines.settings.public()

    @app.put("/api/config")
    async def put_config(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        merged = engines.settings.to_dict()
        for key, value in payload.items():
            if key in merged and value is not None:
                merged[key] = value
        new_settings = config.Settings(**merged)
        # 切换预设时，用预设里的 base_url / 模型 / 密钥环境变量补齐，
        # 但用户在同一个请求里显式写的字段优先级更高。
        new_settings = providers.apply(new_settings, set(payload.keys()))
        config.save(new_settings)
        await engines.update(new_settings)
        return new_settings.public()

    # —— 会话 ——

    @app.get("/api/sessions")
    async def sessions() -> List[Dict[str, Any]]:
        return store.list_sessions(engines.settings.db_path())

    @app.post("/api/sessions")
    async def create_session(payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        current = engines.settings
        return store.create_session(
            current.db_path(),
            title=str(payload.get("title") or ""),
            course=str(payload.get("course") or ""),
            source_lang=str(payload.get("source_lang") or current.source_lang),
            target_lang=str(payload.get("target_lang") or current.target_lang),
        )

    @app.get("/api/sessions/{session_id}")
    async def session_detail(session_id: str) -> Dict[str, Any]:
        db_path = engines.settings.db_path()
        session = store.get_session(db_path, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return {
            "session": session,
            "note": store.get_note(db_path, session_id),
            "summary": store.latest_summary(db_path, session_id),
            "utterances": store.list_utterances(db_path, session_id),
        }

    @app.patch("/api/sessions/{session_id}")
    async def patch_session(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        updated = store.update_session(engines.settings.db_path(), session_id, **payload)
        if updated is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return updated

    @app.delete("/api/sessions/{session_id}")
    async def remove_session(session_id: str) -> Dict[str, Any]:
        if not store.delete_session(engines.settings.db_path(), session_id):
            raise HTTPException(status_code=404, detail="会话不存在")
        return {"deleted": session_id}

    @app.put("/api/sessions/{session_id}/note")
    async def put_note(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        db_path = engines.settings.db_path()
        if store.get_session(db_path, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return store.save_note(db_path, session_id, str(payload.get("content") or ""))

    @app.post("/api/sessions/{session_id}/utterance")
    async def add_utterance(session_id: str, payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        """手工补录一句话。用于：麦克风不可用、课后补记、自动化测试。"""
        db_path = engines.settings.db_path()
        if store.get_session(db_path, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return store.add_utterance(
            db_path,
            session_id,
            source=str(payload.get("source") or ""),
            target=str(payload.get("target") or ""),
            start_ms=int(payload.get("start_ms") or 0),
            end_ms=int(payload.get("end_ms") or 0),
        )

    # —— 总结 ——

    @app.post("/api/sessions/{session_id}/summary")
    async def make_summary(session_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        db_path = engines.settings.db_path()
        session = store.get_session(db_path, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        utterances = store.list_utterances(db_path, session_id)
        if not utterances and not store.get_note(db_path, session_id).strip():
            raise HTTPException(status_code=400, detail="这一节课还没有内容，无法总结")

        kind = str(payload.get("kind") or "full")
        model = engines.settings.resolved_summary_model()
        try:
            content = await asyncio.to_thread(
                summarize,
                engines.client,
                model,
                utterances,
                store.get_note(db_path, session_id),
            )
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        record = store.add_summary(
            db_path,
            session_id,
            content=content,
            kind=kind,
            model=model,
            covers_upto=len(utterances),
        )
        return record

    @app.get("/api/sessions/{session_id}/summary")
    async def get_summary(session_id: str) -> Dict[str, Any]:
        summary = store.latest_summary(engines.settings.db_path(), session_id)
        if summary is None:
            raise HTTPException(status_code=404, detail="还没有总结")
        return summary

    # —— 听课记录（转写 → 结构化笔记）——

    @app.post("/api/sessions/{session_id}/digest")
    async def make_digest(session_id: str, payload: Dict[str, Any] = Body(default={})) -> Dict[str, Any]:
        """把实时转写压成一份能归档的听课记录。

        与「一键总结」的区别：总结是课上的快照（要点/术语/待办），听课记录是
        课后的正式产出——分块抽取再全局合成，篇幅和结构都更完整，带 YAML
        frontmatter，可直接放进 Obsidian。
        """
        db_path = engines.settings.db_path()
        session = store.get_session(db_path, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        utterances = store.list_utterances(db_path, session_id)
        if not utterances:
            raise HTTPException(status_code=400, detail="这节课还没有转写，无法生成听课记录")

        turns = [
            BilingualTurn(
                source=str(item.get("source") or ""),
                target=str(item.get("target") or ""),
                start=(item.get("start_ms") or 0) / 1000,
            )
            for item in utterances
        ]
        budget = int(payload.get("chunk_budget") or 4000)
        try:
            notes = await asyncio.to_thread(digest.build_notes, turns, engines.client, budget)
        except LLMError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        markdown = doc_render.render_markdown(
            notes,
            course=str(session.get("course") or ""),
            source="课堂实时转写（aula）",
            language=str(session.get("source_lang") or engines.settings.source_lang),
            model=engines.settings.resolved_summary_model(),
        )
        handwritten = store.get_note(db_path, session_id).strip()
        if handwritten:
            markdown += "\n## 我的随手记\n\n%s\n" % handwritten

        return {
            "markdown": markdown,
            "notes": notes.to_dict(),
            "model": engines.settings.resolved_summary_model(),
        }

    # —— 导出 ——

    @app.get("/api/sessions/{session_id}/export")
    async def export(session_id: str, transcript: int = Query(1)) -> PlainTextResponse:
        db_path = engines.settings.db_path()
        try:
            markdown = store.export_markdown(db_path, session_id, include_transcript=bool(transcript))
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="会话不存在") from exc
        session = store.get_session(db_path, session_id) or {}
        filename = "aula-%s.md" % str(session.get("title") or session_id).replace(" ", "-")
        return PlainTextResponse(
            markdown,
            media_type="text/markdown; charset=utf-8",
            headers={"Content-Disposition": "attachment; filename*=UTF-8''%s" % _quote(filename)},
        )



    # —— 本机助手（ScreenCaptureKit 系统音频）——
    #
    # macOS 上浏览器抓不到系统级音频，需要原生 API。aula 自带一个单文件
    # Swift 助手（aula/helpers/main.swift）：服务端拉起它，它抓系统播放的
    # 声音、按浏览器同口径切句，再以同样的 WS 协议送回来——前端零改动。

    def _helper_binary() -> Path:
        """助手二进制，没有就当场编译（需要 Xcode Command Line Tools）。"""
        binary = Path(engines.settings.data_dir).expanduser() / "bin" / "aula-system-audio"
        if binary.is_file() and os.access(binary, os.X_OK):
            return binary
        if shutil.which("swiftc") is None:
            raise HTTPException(
                status_code=503,
                detail="本机助手需要 swiftc 编译。装一下 Xcode Command Line Tools（xcode-select --install）"
                       "再试；或者改用「标签页/系统声音」共享（浏览器内直接可用）。",
            )
        if not HELPER_SOURCE.is_file():
            raise HTTPException(status_code=500, detail="助手源码缺失（helpers/main.swift）")
        binary.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory() as tmp:
            tmp_main = Path(tmp) / "main.swift"
            shutil.copy(HELPER_SOURCE, tmp_main)  # 顶层 await 代码必须以 main.swift 名义编译
            result = subprocess.run(
                ["swiftc", "-O", "-swift-version", "5", "-o", str(binary), str(tmp_main)],
                capture_output=True, text=True, timeout=300,
            )
        if result.returncode != 0:
            raise HTTPException(status_code=500, detail="助手编译失败：" + result.stderr[-400:])
        logger.info("本机助手已编译：%s", binary)
        return binary

    @app.post("/api/capture/start")
    async def capture_start(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        session_id = str(payload.get("session_id") or "")
        db_path = engines.settings.db_path()
        if store.get_session(db_path, session_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        existing = _CAPTURES.get(session_id)
        if existing is not None and existing.poll() is None:
            return {"started": True, "already_running": True, "pid": existing.pid}

        binary = _helper_binary()
        url = "ws://127.0.0.1:%d/ws/live?session_id=%s" % (engines.settings.port, session_id)
        log_dir = Path(engines.settings.data_dir).expanduser() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / ("capture-%s.log" % session_id)
        handle = open(log_path, "ab")
        try:
            proc = subprocess.Popen(
                [str(binary), "--url", url, "--verbose"],
                stdout=handle, stderr=handle, start_new_session=True,
            )
        except OSError as exc:
            handle.close()
            raise HTTPException(status_code=500, detail="助手启动失败：" + str(exc)) from exc
        _CAPTURES[session_id] = proc
        logger.info("本机助手已启动（pid=%s，日志 %s）", proc.pid, log_path)
        return {"started": True, "pid": proc.pid, "log": str(log_path)}

    @app.post("/api/capture/stop")
    async def capture_stop(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
        session_id = str(payload.get("session_id") or "")
        proc = _CAPTURES.pop(session_id, None)
        if proc is None:
            return {"stopped": False}
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        return {"stopped": True}

    @app.get("/api/capture/status")
    async def capture_status() -> Dict[str, Any]:
        items = []
        for sid, proc in list(_CAPTURES.items()):
            entry: Dict[str, Any] = {"session_id": sid, "pid": proc.pid, "running": proc.poll() is None}
            if not entry["running"]:
                entry["exit_code"] = proc.returncode
                log_path = Path(engines.settings.data_dir).expanduser() / "logs" / ("capture-%s.log" % sid)
                if log_path.is_file():
                    tail = log_path.read_text(encoding="utf-8", errors="replace").splitlines()[-6:]
                    entry["log_tail"] = tail
                _CAPTURES.pop(sid, None)   # 已退出：清账，下次能重新启动
            items.append(entry)
        binary = Path(engines.settings.data_dir).expanduser() / "bin" / "aula-system-audio"
        return {
            "helper_ready": binary.is_file() and os.access(binary, os.X_OK),
            "swiftc": bool(shutil.which("swiftc")),
            "captures": items,
        }

    # —— 实时链路 ——

    @app.websocket("/ws/live")
    async def live(websocket: WebSocket, session_id: str = Query(...)) -> None:
        await websocket.accept()
        db_path = engines.settings.db_path()
        session = store.get_session(db_path, session_id)
        if session is None:
            await websocket.send_json({"type": "error", "message": "会话不存在"})
            await websocket.close()
            return

        current = engines.settings
        # 先进广播组、再发 ready：客户端拿到 ready 就可能立刻发音频/开始消费，
        # 注册必须发生在那之前，否则开头的几句话会广播不到刚进来的连接。
        _HUBS.setdefault(session_id, set()).add(websocket)
        await websocket.send_json(
            {
                "type": "ready",
                "session": session,
                "asr": engines.asr.describe(),
                "translate": engines.translator.enabled,
                "source_lang": current.source_lang,
                "target_lang": current.target_lang,
                "sample_rate": current.sample_rate,
                "vad": {
                    "silence_ms": current.silence_ms,
                    "min_speech_ms": current.min_speech_ms,
                    "max_segment_ms": current.max_segment_ms,
                },
                "auto_summary_minutes": current.auto_summary_minutes,
                "last_seq": max([u["seq"] for u in store.list_utterances(db_path, session_id)] or [0]),
            }
        )

        queue: "asyncio.Queue[Optional[tuple]]" = asyncio.Queue(maxsize=64)
        stop = asyncio.Event()
        # 流式草稿：客户端每 ~0.9 秒推一次"说到现在的音频"，这里只留最新一份；
        # partial_worker 立即转写并广播。同一句话的草稿逐帧覆盖，永远只算最新。
        latest_partial: Dict[str, Any] = {"pcm": None, "start_ms": 0, "end_ms": 0, "seq_id": 0}
        partial_done = {"seq_id": 0}

        async def partial_worker() -> None:
            while not stop.is_set():
                await asyncio.sleep(0.12)
                if latest_partial["seq_id"] <= partial_done["seq_id"]:
                    continue
                partial_done["seq_id"] = latest_partial["seq_id"]
                pcm = latest_partial["pcm"]
                if pcm is None:
                    continue
                # 定稿优先：定稿在队列里等着，草稿让路（马上会被定稿覆盖）
                if not queue.empty():
                    continue
                try:
                    async with engines.asr_lock:
                        result, _ = await engines.asr.transcribe(
                            pcm, current.sample_rate, current.source_lang
                        )
                except ASRError:
                    continue
                text = (result.text or "").strip()
                if not text:
                    continue
                # 草稿只推原文。草稿阶段的粗译会立刻被定稿覆盖，字幕上就是
                # "闪一下再变"——这正是要消掉的抖动。译文统一走定稿后的流式增量。
                await _broadcast(
                    session_id,
                    {"type": "partial", "key": latest_partial["start_ms"],
                     "source": text, "target": ""},
                )

        async def stream_translate(seg_text: str, seq: int) -> str:
            """把精译的增量逐个广播出去，返回拼好的全文。

            线程模型：``translate_stream`` 是阻塞的（urllib 逐行读 SSE），跑在
            ``to_thread`` 的工作线程里；增量用 ``call_soon_threadsafe`` 回到事件循环，
            再由 ``_broadcast`` 发给这节课的所有连接。
            """
            loop = asyncio.get_running_loop()
            queue: "asyncio.Queue[object]" = asyncio.Queue()
            done_marker = object()

            def emit(delta: str) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, delta)

            def work() -> str:
                try:
                    return engines.translator.translate_stream(seg_text, emit)
                finally:
                    loop.call_soon_threadsafe(queue.put_nowait, done_marker)

            runner = asyncio.create_task(asyncio.to_thread(work))
            collected: list = []
            while True:
                item = await queue.get()
                if item is done_marker:
                    break
                collected.append(item)
                await _broadcast(
                    session_id,
                    {"type": "translation_delta", "seq": seq, "delta": item},
                )
            try:
                return await runner
            except Exception as exc:  # noqa: BLE001
                logger.warning("流式精译异常：%s", exc)
                return "".join(collected)

        async def worker() -> None:
            while not stop.is_set():
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                pcm, start_ms, end_ms = item
                try:
                    async with engines.asr_lock:
                        result, asr_ms = await engines.asr.transcribe(
                            pcm, current.sample_rate, current.source_lang
                        )
                except ASRError as exc:
                    await _broadcast(session_id, {"type": "error", "message": str(exc), "stage": "asr"})
                    queue.task_done()
                    continue

                text = (result.text or "").strip()
                if not text:
                    queue.task_done()
                    continue

                # 有 LLM 就走流式精译：transcript 里不带译文，译文由随后的
                # translation_delta 逐字补上——屏幕上只有"长出来"，没有"被替换"。
                # 没有 LLM 时才退回专用翻译接口（一次性返回，无增量）。
                uses_llm = engines.translator.enabled
                fallback_text = ""
                if not uses_llm and engines.fast_mt is not None:
                    try:
                        fallback_text = await asyncio.to_thread(
                            engines.fast_mt.translate, text,
                            engines.settings.source_lang, engines.settings.target_lang,
                        )
                    except Exception:  # noqa: BLE001
                        fallback_text = ""
                record = store.add_utterance(
                    db_path,
                    session_id,
                    source=text,
                    target=fallback_text,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    confidence=result.confidence,
                    latency_ms=asr_ms,
                )
                await _broadcast(
                    session_id,
                    {
                        "type": "transcript",
                        "utterance": record,
                        "latency_ms": asr_ms,
                        "provider": result.provider,
                        "key": start_ms,
                    },
                )

                if uses_llm:
                    translated = await stream_translate(text, record["seq"])
                    if translated:
                        store.set_translation(db_path, session_id, record["seq"], translated)
                        # 收尾的完整文本：客户端漏接 delta 时用它兜底（幂等覆盖）
                        await _broadcast(
                            session_id,
                            {"type": "translation", "seq": record["seq"], "target": translated},
                        )
                queue.task_done()
            stop.set()

        async def auto_summary() -> None:
            minutes = current.auto_summary_minutes
            if minutes <= 0:
                return
            while not stop.is_set():
                try:
                    await asyncio.wait_for(stop.wait(), timeout=minutes * 60)
                    return
                except asyncio.TimeoutError:
                    pass
                count = store.count_utterances(db_path, session_id)
                if count == 0:
                    continue
                try:
                    content = await asyncio.to_thread(
                        summarize,
                        engines.client,
                        current.resolved_summary_model(),
                        store.list_utterances(db_path, session_id),
                        store.get_note(db_path, session_id),
                    )
                except LLMError as exc:
                    await _broadcast(session_id, {"type": "error", "message": str(exc), "stage": "summary"})
                    continue
                record = store.add_summary(
                    db_path,
                    session_id,
                    content=content,
                    kind="auto",
                    model=current.resolved_summary_model(),
                    covers_upto=count,
                )
                await _broadcast(session_id, {"type": "summary", "summary": record})

        worker_task = asyncio.create_task(worker())
        summary_task = asyncio.create_task(auto_summary())
        partial_task = asyncio.create_task(partial_worker())

        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                raw = message.get("bytes")
                if raw is not None:
                    # 帧头：<II> 8 字节（旧版，flags=0）或 <III> 12 字节（flags bit0=草稿）
                    if len(raw) >= 12:
                        start_ms, end_ms, flags = struct.unpack("<III", raw[:12])
                        pcm = raw[12:]
                    elif len(raw) > 8:
                        start_ms, end_ms = struct.unpack("<II", raw[:8])
                        flags = 0
                        pcm = raw[8:]
                    else:
                        continue
                    if len(pcm) < current.sample_rate // 2:  # <0.25 秒，丢弃
                        continue
                    if flags & 1:
                        # 草稿帧：不入库不排队，直接刷新"最新草稿"，由
                        # partial_worker 立即转写——YouTube 式边说边出字。
                        if len(pcm) < current.sample_rate:  # 草稿至少 0.5 秒，太短没意义
                            continue
                        latest_partial["pcm"] = pcm
                        latest_partial["start_ms"] = start_ms
                        latest_partial["end_ms"] = end_ms
                        latest_partial["seq_id"] += 1
                        continue
                    if queue.full():
                        await _broadcast(session_id, {"type": "status", "message": "处理不过来，丢弃一段"})
                        continue
                    await queue.put((pcm, start_ms, end_ms))
                    continue

                text_frame = message.get("text")
                if text_frame:
                    if '"ping"' in text_frame:
                        await _broadcast(session_id, {"type": "pong"})
                    elif '"flush"' in text_frame:
                        await queue.join()
                        await _broadcast(session_id, {"type": "flushed"})
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()
            await queue.put(None)
            for task in (worker_task, summary_task, partial_task):
                task.cancel()
            await asyncio.gather(worker_task, summary_task, partial_task, return_exceptions=True)
            _HUBS.get(session_id, set()).discard(websocket)
            if not _HUBS.get(session_id):
                _HUBS.pop(session_id, None)

    return app



def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text)


async def _broadcast(session_id: str, payload: Dict[str, Any]) -> None:
    """把一条消息发给这节课的**所有**连接。

    掉线的连接在这里摘除而不是触发整组失败——一个没关干净的旧标签页
    不该把正在上课的那个页面带崩。
    """
    for socket in list(_HUBS.get(session_id, ())):
        try:
            await socket.send_json(payload)
        except Exception:  # noqa: BLE001
            _HUBS.get(session_id, set()).discard(socket)
