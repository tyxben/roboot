"""Chat history persistence.

Single SQLite file at `.chat_history.db` (gitignored). Both the local ws
endpoint and the relay client write through this module so transcripts
survive daemon restarts, browser refreshes, and mobile reconnects.

Schema:
  sessions(session_id TEXT PK, source TEXT, label TEXT, created_at, updated_at)
  messages(id INTEGER PK, session_id TEXT FK, role TEXT, content TEXT,
           tools_used INTEGER, created_at)

`source` is one of: 'local', 'remote', 'telegram'. The DB is write-mostly
from the agent's perspective; readers (future UI / CLI / resume) will
query by source + recency.

Operations happen via asyncio.to_thread so the asyncio event loop never
blocks on sqlite; connections are re-opened per call (cheap with
SQLite + PRAGMA journal_mode=WAL so concurrent readers are fine).

Retention: messages older than RETENTION_DAYS are purged on each
create_session() call. Sessions with no remaining messages are also
dropped. Default 30 days, override with ROBOOT_CHAT_RETENTION_DAYS env.
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import time
import uuid
from pathlib import Path

DB_PATH = Path(__file__).parent / ".chat_history.db"
RETENTION_DAYS = int(os.environ.get("ROBOOT_CHAT_RETENTION_DAYS", "30"))

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS sessions (
  session_id TEXT PRIMARY KEY,
  source     TEXT NOT NULL,
  label      TEXT,
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL,
  role       TEXT NOT NULL,
  content    TEXT NOT NULL,
  tools_used INTEGER DEFAULT 0,
  created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session
  ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_source_updated
  ON sessions(source, updated_at DESC);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_INIT_SQL)
    return conn


def _purge_old(conn: sqlite3.Connection) -> None:
    cutoff = time.time() - RETENTION_DAYS * 86400
    conn.execute("DELETE FROM messages WHERE created_at < ?", (cutoff,))
    conn.execute(
        """DELETE FROM sessions WHERE session_id NOT IN
           (SELECT DISTINCT session_id FROM messages)
           AND created_at < ?""",
        (cutoff,),
    )


def _create_session_sync(source: str, label: str | None) -> str:
    session_id = str(uuid.uuid4())
    now = time.time()
    with _connect() as conn:
        _purge_old(conn)
        conn.execute(
            "INSERT INTO sessions(session_id, source, label, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, source, label, now, now),
        )
    return session_id


def _record_sync(session_id: str, role: str, content: str, tools_used: int) -> None:
    now = time.time()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO messages(session_id, role, content, tools_used, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (session_id, role, content, tools_used, now),
        )
        conn.execute(
            "UPDATE sessions SET updated_at=? WHERE session_id=?",
            (now, session_id),
        )


def _list_messages_sync(session_id: str, limit: int) -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            """SELECT role, content, tools_used, created_at FROM messages
               WHERE session_id=? ORDER BY created_at DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
    # Return oldest-first so callers can feed them as context in order.
    return [
        {"role": r[0], "content": r[1], "tools_used": r[2], "created_at": r[3]}
        for r in reversed(rows)
    ]


async def create_session(source: str, label: str | None = None) -> str:
    """Create a new chat session row. Returns its session_id."""
    return await asyncio.to_thread(_create_session_sync, source, label)


async def record_user(session_id: str, content: str) -> None:
    if not session_id or not content:
        return
    await asyncio.to_thread(_record_sync, session_id, "user", content, 0)


async def record_assistant(session_id: str, content: str, tools_used: int = 0) -> None:
    # Empty replies (e.g. model errored mid-stream) — still record as a
    # zero-length row so the transcript order isn't ambiguous.
    if not session_id:
        return
    await asyncio.to_thread(_record_sync, session_id, "assistant", content, tools_used)


async def list_messages(session_id: str, limit: int = 200) -> list[dict]:
    """Oldest-first list of recent messages in a session."""
    return await asyncio.to_thread(_list_messages_sync, session_id, limit)


def _wipe_all_sync() -> int:
    conn = _connect()
    try:
        row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
        deleted = int(row[0]) if row else 0
        conn.execute("DELETE FROM messages")
        conn.execute("DELETE FROM sessions")
        # VACUUM must not run inside a transaction. _connect() uses
        # isolation_level=None (autocommit) so the DELETEs above are
        # already committed, and VACUUM will reclaim pages so deleted
        # rows aren't recoverable from the file.
        conn.execute("VACUUM")
    finally:
        conn.close()
    return deleted


async def wipe_all() -> int:
    """Delete every session and message row, then VACUUM so the on-disk
    file actually shrinks. Returns the number of messages deleted — the
    console surfaces this in a toast so the user knows it did something.

    Active WS connections keep their in-memory session_id; subsequent
    record_user/record_assistant calls will silently re-insert into the
    newly-empty tables (no FK enforcement, so this is safe)."""
    return await asyncio.to_thread(_wipe_all_sync)
