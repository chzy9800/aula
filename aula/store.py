"""存储：本地 SQLite，一次课一个会话。

为什么是 SQLite 而不是 JSON 文件：课堂转写是逐句追加的，一节课下来几百条，
JSON 要么全量重写（写放大），要么自己做追加式格式（等于手写数据库）。
SQLite 是标准库自带、单文件、能并发读、崩了也不容易坏。

隐私取向：数据只落在 ``~/.escucha``，没有上传逻辑。这对课堂录音是硬要求。
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    title       TEXT NOT NULL,
    course      TEXT NOT NULL DEFAULT '',
    source_lang TEXT NOT NULL DEFAULT 'es',
    target_lang TEXT NOT NULL DEFAULT 'zh',
    status      TEXT NOT NULL DEFAULT 'active',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS utterances (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    start_ms    INTEGER NOT NULL DEFAULT 0,
    end_ms      INTEGER NOT NULL DEFAULT 0,
    source      TEXT NOT NULL DEFAULT '',
    target      TEXT NOT NULL DEFAULT '',
    confidence  REAL,
    latency_ms  INTEGER,
    speaker     TEXT,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_utterances_session ON utterances(session_id, seq);

CREATE TABLE IF NOT EXISTS notes (
    session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
    content    TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL DEFAULT 'full',
    model       TEXT NOT NULL DEFAULT '',
    content     TEXT NOT NULL DEFAULT '{}',
    covers_upto INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id, id DESC);
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """每次操作开一个连接，用完关掉。

    sqlite3 的 ``with conn:`` 只负责事务提交/回滚，**不会关闭连接**，用完不
    close 会泄漏文件句柄（Python 3.13 会直接抛 ResourceWarning）。
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
    finally:
        conn.close()


def init_db(db_path: Path) -> None:
    with _connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # 必须在 with 内：出了这个块 conn 已关闭
        _ensure_utterance_columns(conn)


def _row(row: Optional[sqlite3.Row]) -> Optional[Dict[str, Any]]:
    return dict(row) if row is not None else None


# —— 会话 ——


def create_session(
    db_path: Path,
    title: str = "",
    course: str = "",
    source_lang: str = "es",
    target_lang: str = "zh",
) -> Dict[str, Any]:
    session_id = uuid.uuid4().hex[:12]
    stamp = _now()
    title = title.strip() or datetime.now().strftime("%m-%d %H:%M 的课")
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, course, source_lang, target_lang, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
            (session_id, title, course, source_lang, target_lang, stamp, stamp),
        )
        conn.execute(
            "INSERT INTO notes (session_id, content, updated_at) VALUES (?, '', ?)",
            (session_id, stamp),
        )
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    result = _row(row) or {}
    result["utterance_count"] = 0
    return result


def list_sessions(db_path: Path, limit: int = 100) -> List[Dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT s.*, (SELECT COUNT(*) FROM utterances u WHERE u.session_id = s.id) AS utterance_count
            FROM sessions s
            ORDER BY s.updated_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_session(db_path: Path, session_id: str) -> Optional[Dict[str, Any]]:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["utterance_count"] = conn.execute(
            "SELECT COUNT(*) FROM utterances WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
    return data


def update_session(db_path: Path, session_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
    allowed = {"title", "course", "status", "source_lang", "target_lang"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return get_session(db_path, session_id)
    assignments = ", ".join("%s = ?" % key for key in updates)
    values = list(updates.values()) + [_now(), session_id]
    with _connect(db_path) as conn:
        conn.execute(
            "UPDATE sessions SET %s, updated_at = ? WHERE id = ?" % assignments,
            values,
        )
    return get_session(db_path, session_id)


def delete_session(db_path: Path, session_id: str) -> bool:
    with _connect(db_path) as conn:
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cursor.rowcount > 0


# —— 转写 ——


def _ensure_utterance_columns(conn: "sqlite3.Connection") -> None:
    """给老库补 speaker 列。SQLite 没有 IF NOT EXISTS for column，只能先查。"""
    columns = {row[1] for row in conn.execute("PRAGMA table_info(utterances)")}
    if "speaker" not in columns:
        conn.execute("ALTER TABLE utterances ADD COLUMN speaker TEXT")


def add_utterance(
    db_path: Path,
    session_id: str,
    source: str,
    target: str = "",
    start_ms: int = 0,
    end_ms: int = 0,
    confidence: Optional[float] = None,
    latency_ms: Optional[int] = None,
    speaker: str = "",
) -> Dict[str, Any]:
    stamp = _now()
    with _connect(db_path) as conn:
        seq = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 FROM utterances WHERE session_id = ?", (session_id,)
        ).fetchone()[0]
        cursor = conn.execute(
            "INSERT INTO utterances (session_id, seq, start_ms, end_ms, source, target, confidence, latency_ms, speaker, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session_id, seq, start_ms, end_ms, source, target, confidence, latency_ms,
             speaker or None, stamp),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id))
        row = conn.execute("SELECT * FROM utterances WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row) if row else {}


def set_translation(db_path: Path, session_id: str, seq: int, target: str) -> bool:
    """回填译文。

    为什么单独一个接口：原文与译文**不再同时入库**。ASR 一完成就先落库并推给
    前端（用户约 1 秒就能看到听到了什么），译文随后补上——翻译为了带上下文判
    断残句，实测要多花约 0.7 秒。让用户等两个都齐了才看到东西，是拿感知延迟
    换了个看不见的好处。
    """
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "UPDATE utterances SET target = ? WHERE session_id = ? AND seq = ?",
            (target, session_id, seq),
        )
    return cursor.rowcount > 0


def list_utterances(db_path: Path, session_id: str, after_seq: int = 0) -> List[Dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM utterances WHERE session_id = ? AND seq > ? ORDER BY seq ASC",
            (session_id, after_seq),
        ).fetchall()
    return [dict(r) for r in rows]


def count_utterances(db_path: Path, session_id: str) -> int:
    with _connect(db_path) as conn:
        return int(
            conn.execute("SELECT COUNT(*) FROM utterances WHERE session_id = ?", (session_id,)).fetchone()[0]
        )


# —— 笔记 ——


def save_note(db_path: Path, session_id: str, content: str) -> Dict[str, Any]:
    stamp = _now()
    with _connect(db_path) as conn:
        conn.execute(
            "INSERT INTO notes (session_id, content, updated_at) VALUES (?, ?, ?)"
            " ON CONFLICT(session_id) DO UPDATE SET content = excluded.content, updated_at = excluded.updated_at",
            (session_id, content, stamp),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id))
    return {"session_id": session_id, "content": content, "updated_at": stamp}


def get_note(db_path: Path, session_id: str) -> str:
    with _connect(db_path) as conn:
        row = conn.execute("SELECT content FROM notes WHERE session_id = ?", (session_id,)).fetchone()
    return row["content"] if row else ""


# —— 总结 ——


def add_summary(
    db_path: Path,
    session_id: str,
    content: Dict[str, Any],
    kind: str = "full",
    model: str = "",
    covers_upto: int = 0,
) -> Dict[str, Any]:
    stamp = _now()
    payload = json.dumps(content, ensure_ascii=False)
    with _connect(db_path) as conn:
        cursor = conn.execute(
            "INSERT INTO summaries (session_id, kind, model, content, covers_upto, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, kind, model, payload, covers_upto, stamp),
        )
        conn.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (stamp, session_id))
        row = conn.execute("SELECT * FROM summaries WHERE id = ?", (cursor.lastrowid,)).fetchone()
    return _decode_summary(dict(row)) if row else {}


def latest_summary(db_path: Path, session_id: str) -> Optional[Dict[str, Any]]:
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT 1", (session_id,)
        ).fetchone()
    return _decode_summary(dict(row)) if row else None


def list_summaries(db_path: Path, session_id: str, limit: int = 20) -> List[Dict[str, Any]]:
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM summaries WHERE session_id = ? ORDER BY id DESC LIMIT ?",
            (session_id, limit),
        ).fetchall()
    return [_decode_summary(dict(r)) for r in rows]


def _decode_summary(data: Dict[str, Any]) -> Dict[str, Any]:
    try:
        data["content"] = json.loads(data.get("content") or "{}")
    except json.JSONDecodeError:
        data["content"] = {"raw": data.get("content", "")}
    return data


# —— 导出 ——


def export_markdown(db_path: Path, session_id: str, include_transcript: bool = True) -> str:
    """导出成一份完整课堂记录：总结在前，笔记居中，转写附后。"""
    from .render import render_export

    session = get_session(db_path, session_id)
    if session is None:
        raise KeyError(session_id)
    return render_export(
        session=session,
        note=get_note(db_path, session_id),
        summary=latest_summary(db_path, session_id),
        utterances=list_utterances(db_path, session_id) if include_transcript else [],
    )
