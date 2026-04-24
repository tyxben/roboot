"""Smoke tests for chat_store.py.

Each test monkeypatches `chat_store.DB_PATH` to a per-test tmp_path so the
real `.chat_history.db` is never touched.
"""

from __future__ import annotations

import time

import pytest

import chat_store


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    """Redirect chat_store at a fresh SQLite file and reset retention."""
    db = tmp_path / "chat_history.db"
    monkeypatch.setattr(chat_store, "DB_PATH", db)
    # Default retention matches module default; tests that need fake time
    # monkeypatch time.time directly.
    monkeypatch.setattr(chat_store, "RETENTION_DAYS", 30)
    return db


async def test_create_session_returns_uuid_and_registers_row(tmp_db):
    sid = await chat_store.create_session("local", label="hello")
    # uuid4 format: 36 chars with dashes in the right places
    assert isinstance(sid, str)
    assert len(sid) == 36
    assert sid.count("-") == 4

    import sqlite3

    conn = sqlite3.connect(tmp_db)
    row = conn.execute(
        "SELECT source, label FROM sessions WHERE session_id=?", (sid,)
    ).fetchone()
    conn.close()
    assert row == ("local", "hello")


async def test_record_user_and_assistant_append_in_order(tmp_db):
    sid = await chat_store.create_session("local")
    await chat_store.record_user(sid, "hi")
    await chat_store.record_assistant(sid, "hello back", tools_used=2)

    msgs = await chat_store.list_messages(sid)
    assert len(msgs) == 2
    assert msgs[0]["role"] == "user"
    assert msgs[0]["content"] == "hi"
    assert msgs[0]["tools_used"] == 0
    assert msgs[1]["role"] == "assistant"
    assert msgs[1]["content"] == "hello back"
    assert msgs[1]["tools_used"] == 2
    # Oldest-first ordering by created_at
    assert msgs[0]["created_at"] <= msgs[1]["created_at"]


async def test_list_messages_oldest_first(tmp_db):
    sid = await chat_store.create_session("remote")
    for i in range(5):
        await chat_store.record_user(sid, f"msg {i}")

    msgs = await chat_store.list_messages(sid)
    assert [m["content"] for m in msgs] == [f"msg {i}" for i in range(5)]


async def test_list_messages_respects_limit(tmp_db):
    sid = await chat_store.create_session("local")
    for i in range(10):
        await chat_store.record_user(sid, f"m{i}")
    # list_messages takes the most recent `limit` rows, returned oldest-first.
    msgs = await chat_store.list_messages(sid, limit=3)
    assert [m["content"] for m in msgs] == ["m7", "m8", "m9"]


async def test_record_with_empty_content_is_ignored(tmp_db):
    sid = await chat_store.create_session("local")
    await chat_store.record_user(sid, "")  # empty -> no-op
    await chat_store.record_user(sid, "real")
    msgs = await chat_store.list_messages(sid)
    assert len(msgs) == 1
    assert msgs[0]["content"] == "real"


async def test_record_assistant_allows_empty(tmp_db):
    # Empty assistant replies are still recorded (mid-stream errors).
    sid = await chat_store.create_session("local")
    await chat_store.record_assistant(sid, "", tools_used=0)
    msgs = await chat_store.list_messages(sid)
    assert len(msgs) == 1
    assert msgs[0]["role"] == "assistant"
    assert msgs[0]["content"] == ""


async def test_sessions_do_not_cross_contaminate(tmp_db):
    sid_a = await chat_store.create_session("local", label="A")
    sid_b = await chat_store.create_session("remote", label="B")

    await chat_store.record_user(sid_a, "from A")
    await chat_store.record_user(sid_b, "from B")

    msgs_a = await chat_store.list_messages(sid_a)
    msgs_b = await chat_store.list_messages(sid_b)

    assert [m["content"] for m in msgs_a] == ["from A"]
    assert [m["content"] for m in msgs_b] == ["from B"]


async def test_wipe_all_clears_messages_and_sessions(tmp_db):
    # Populate two sessions across sources so we cover the multi-row path.
    sid_a = await chat_store.create_session("local", label="A")
    sid_b = await chat_store.create_session("remote", label="B")
    await chat_store.record_user(sid_a, "a1")
    await chat_store.record_assistant(sid_a, "a2", tools_used=1)
    await chat_store.record_user(sid_b, "b1")

    deleted = await chat_store.wipe_all()
    assert deleted == 3

    import sqlite3

    conn = sqlite3.connect(tmp_db)
    msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    sess_count = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    conn.close()
    assert msg_count == 0
    assert sess_count == 0

    # Creating a new session after wipe must still work (schema intact).
    sid_fresh = await chat_store.create_session("local")
    await chat_store.record_user(sid_fresh, "post-wipe")
    msgs = await chat_store.list_messages(sid_fresh)
    assert [m["content"] for m in msgs] == ["post-wipe"]


async def test_wipe_all_on_empty_db_returns_zero(tmp_db):
    # No sessions, no messages — wipe is still safe.
    deleted = await chat_store.wipe_all()
    assert deleted == 0


async def test_retention_purge_drops_old_sessions_on_create(tmp_db, monkeypatch):
    # Freeze time to "40 days ago", create + populate an old session.
    real_time = time.time
    old_t = real_time() - 40 * 86400
    monkeypatch.setattr(chat_store.time, "time", lambda: old_t)

    sid_old = await chat_store.create_session("local", label="stale")
    await chat_store.record_user(sid_old, "ancient")

    # Sanity: old session exists before purge.
    msgs_before = await chat_store.list_messages(sid_old)
    assert len(msgs_before) == 1

    # Advance clock back to "now" and create a fresh session — this triggers
    # _purge_old, which drops rows older than RETENTION_DAYS (30 days).
    monkeypatch.setattr(chat_store.time, "time", real_time)
    sid_new = await chat_store.create_session("local", label="fresh")
    await chat_store.record_user(sid_new, "current")

    # Old messages gone; old session row also dropped.
    msgs_old = await chat_store.list_messages(sid_old)
    assert msgs_old == []

    import sqlite3

    conn = sqlite3.connect(tmp_db)
    old_rows = conn.execute(
        "SELECT 1 FROM sessions WHERE session_id=?", (sid_old,)
    ).fetchall()
    conn.close()
    assert old_rows == []

    # Fresh session untouched.
    msgs_new = await chat_store.list_messages(sid_new)
    assert [m["content"] for m in msgs_new] == ["current"]
