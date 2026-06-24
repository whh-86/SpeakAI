import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB_PATH = Path(__file__).parent.parent / "instance" / "speakai.db"


def _db_path() -> Path:
    return Path(os.getenv("SPEAKAI_DB_PATH", str(_DEFAULT_DB_PATH)))


def get_connection():
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    _db_path().parent.mkdir(parents=True, exist_ok=True)
    with get_connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS deleted_sessions (
                id TEXT PRIMARY KEY,
                deleted_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New Conversation',
                level TEXT DEFAULT 'B',
                turns INTEGER DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                audio_url TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                original TEXT,
                corrected TEXT,
                reason TEXT,
                error_type TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS pronunciation_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                score_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
            );
        """)
        try:
            conn.execute("ALTER TABLE messages ADD COLUMN audio_url TEXT")
        except Exception:
            pass
        try:
            conn.execute("ALTER TABLE corrections ADD COLUMN msg_id INTEGER")
        except Exception:
            pass


def create_session() -> dict:
    session_id = str(uuid.uuid4())
    now = _now()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sessions (id, title, level, turns, created_at, updated_at) VALUES (?, 'New Conversation', 'B', 0, ?, ?)",
            (session_id, now, now),
        )
    return {"id": session_id, "title": "New Conversation", "turns": 0, "level": "B", "created_at": now, "updated_at": now}


def ensure_session(session_id: str) -> None:
    if is_deleted_session(session_id):
        return
    now = _now()
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO sessions (id, title, level, turns, created_at, updated_at) VALUES (?, 'New Conversation', 'B', 0, ?, ?)",
            (session_id, now, now),
        )


def list_sessions() -> list:
    with get_connection() as conn:
        rows = conn.execute(
            """
            SELECT id, title, level, turns, created_at, updated_at
            FROM sessions
            WHERE id NOT IN (SELECT id FROM deleted_sessions)
            ORDER BY updated_at DESC
            """
        ).fetchall()
    return [dict(r) for r in rows]


def get_session_data(session_id: str) -> dict | None:
    if is_deleted_session(session_id):
        return None
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
        if not row:
            return None
        data = dict(row)
        msgs = conn.execute(
            "SELECT id, role, content, audio_url, created_at FROM messages WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        corrs = conn.execute(
            "SELECT original, corrected, reason, error_type as type, msg_id, created_at FROM corrections WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        corr_by_msg_id: dict[int, list] = {}
        corr_by_ts: dict[str, list] = {}
        for c in corrs:
            cd = {"original": c["original"], "corrected": c["corrected"], "reason": c["reason"], "type": c["type"]}
            if c["msg_id"]:
                corr_by_msg_id.setdefault(c["msg_id"], []).append(cd)
            else:
                corr_by_ts.setdefault(c["created_at"], []).append(cd)
        messages = []
        for msg in msgs:
            m: dict = {"role": msg["role"], "text": msg["content"], "audio_url": msg["audio_url"]}
            if msg["role"] == "assistant":
                by_id = corr_by_msg_id.get(msg["id"], [])
                by_ts = corr_by_ts.get(msg["created_at"], [])
                m["corrections"] = by_id + by_ts
            messages.append(m)
        data["messages"] = messages
        data["corrections"] = [{"original": c["original"], "corrected": c["corrected"], "reason": c["reason"], "type": c["type"]} for c in corrs]
        prons = conn.execute(
            "SELECT score_json FROM pronunciation_scores WHERE session_id = ? ORDER BY id",
            (session_id,),
        ).fetchall()
        data["pronunciation_scores"] = [json.loads(r["score_json"]) for r in prons]
    return data


def save_turn(
    session_id: str,
    user_text: str,
    ai_text: str,
    corrections: list,
    pronunciation: dict,
    level: str,
    user_audio_url: str | None = None,
    ai_audio_url: str | None = None,
) -> None:
    if is_deleted_session(session_id):
        return
    now = _now()
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO messages (session_id, role, content, audio_url, created_at) VALUES (?, 'user', ?, ?, ?)",
            (session_id, user_text, user_audio_url, now),
        )
        cursor = conn.execute(
            "INSERT INTO messages (session_id, role, content, audio_url, created_at) VALUES (?, 'assistant', ?, ?, ?)",
            (session_id, ai_text, ai_audio_url, now),
        )
        ai_msg_id = cursor.lastrowid
        for c in corrections:
            conn.execute(
                "INSERT INTO corrections (session_id, original, corrected, reason, error_type, msg_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (session_id, c.get("original", ""), c.get("corrected", ""), c.get("reason", ""), c.get("type", "other"), ai_msg_id, now),
            )
        conn.execute(
            "INSERT INTO pronunciation_scores (session_id, score_json, created_at) VALUES (?, ?, ?)",
            (session_id, json.dumps(pronunciation), now),
        )
        conn.execute(
            "UPDATE sessions SET turns = turns + 1, level = ?, updated_at = ? WHERE id = ?",
            (level, now, session_id),
        )


def set_title(session_id: str, title: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, session_id))


def delete_session(session_id: str) -> bool:
    now = _now()
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO deleted_sessions (id, deleted_at) VALUES (?, ?)",
            (session_id, now),
        )
        cursor = conn.execute("DELETE FROM sessions WHERE id = ?", (session_id,))
    return cursor.rowcount > 0


def is_deleted_session(session_id: str) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT 1 FROM deleted_sessions WHERE id = ?", (session_id,)).fetchone()
    return row is not None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
