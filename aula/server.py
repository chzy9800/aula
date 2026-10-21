"""HTTP 与 WebSocket 服务。

实时链路的协议（刻意做得很薄，方便你自己写客户端）：

    客户端 → 服务端
      文本帧  {"type": "ping"}                       保活
      文本帧  {"type": "flush"}                      强制把队列里剩下的处理完
      二进制帧 前 8 字节 = start_ms、end_ms（各 uint32 小端）+ 之后是 PCM16

    服务端 → 客户端
      {"type": "ready", ...}                         连接就绪，带上生效的模型
      {"type": "transcript", ...}                    一句话的转写 + 译文
      {"type": "summary", ...}                       自动总结推送
      {"type": "status", ...}                        队列状态
      {"type": "error", "message": "..."}             出错但不掉线

为什么用二进制帧而不是 base64：一段 5 秒 16kHz PCM 是 160KB，base64 后
膨胀 33%，而且浏览器还要多做一次编解码。二进制直接扔过去。
"""

from __future__ import annotations

import asyncio
import struct
from pathlib import Path
from typing import Any, Dict, List, Optional

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


class Engines:
    """把模型实例集中管理，配置变更时整体重建。"""

    def __init__(self, settings: config.Settings) -> None:
        self._lock = asyncio.Lock()
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

        async def worker() -> None:
            while not stop.is_set():
                item = await queue.get()
                if item is None:
                    queue.task_done()
                    break
                pcm, start_ms, end_ms = item
                try:
                    result, asr_ms = await engines.asr.transcribe(
                        pcm, current.sample_rate, current.source_lang
                    )
                except ASRError as exc:
                    await websocket.send_json({"type": "error", "message": str(exc), "stage": "asr"})
                    queue.task_done()
                    continue

                text = (result.text or "").strip()
                if not text:
                    queue.task_done()
                    continue

                translated = ""
                if engines.translator.enabled:
                    translated = await asyncio.to_thread(engines.translator.translate, text)

                record = store.add_utterance(
                    db_path,
                    session_id,
                    source=text,
                    target=translated,
                    start_ms=start_ms,
                    end_ms=end_ms,
                    confidence=result.confidence,
                    latency_ms=asr_ms,
                )
                await websocket.send_json(
                    {
                        "type": "transcript",
                        "utterance": record,
                        "latency_ms": asr_ms,
                        "provider": result.provider,
                    }
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
                    await websocket.send_json({"type": "error", "message": str(exc), "stage": "summary"})
                    continue
                record = store.add_summary(
                    db_path,
                    session_id,
                    content=content,
                    kind="auto",
                    model=current.resolved_summary_model(),
                    covers_upto=count,
                )
                await websocket.send_json({"type": "summary", "summary": record})

        worker_task = asyncio.create_task(worker())
        summary_task = asyncio.create_task(auto_summary())

        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                raw = message.get("bytes")
                if raw is not None:
                    if len(raw) <= 8:
                        continue
                    start_ms, end_ms = struct.unpack("<II", raw[:8])
                    pcm = raw[8:]
                    if len(pcm) < current.sample_rate:  # <0.5 秒，丢弃
                        continue
                    if queue.full():
                        await websocket.send_json({"type": "status", "message": "处理不过来，丢弃一段"})
                        continue
                    await queue.put((pcm, start_ms, end_ms))
                    continue

                text_frame = message.get("text")
                if text_frame:
                    if '"ping"' in text_frame:
                        await websocket.send_json({"type": "pong"})
                    elif '"flush"' in text_frame:
                        await queue.join()
                        await websocket.send_json({"type": "flushed"})
        except WebSocketDisconnect:
            pass
        finally:
            stop.set()
            await queue.put(None)
            for task in (worker_task, summary_task):
                task.cancel()
            await asyncio.gather(worker_task, summary_task, return_exceptions=True)

    return app


def _quote(text: str) -> str:
    from urllib.parse import quote

    return quote(text)
