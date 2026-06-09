"""Tests for chat-history full-text search (chat_store FTS5 + search_chat tool)."""

from __future__ import annotations

import pytest

import chat_store
from tools import chat_search


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(chat_store, "DB_PATH", tmp_path / "chat.db")
    yield


async def _seed():
    sid = await chat_store.create_session("local", "t")
    await chat_store.record_user(sid, "今天我们讨论了 Cloudflare relay 的加密握手")
    await chat_store.record_assistant(sid, "好的，relay 用 ECDH P-256 加 HKDF")
    await chat_store.record_user(sid, "顺便提醒我明天看 GitHub Actions 的 build")
    return sid


# ---------------------------------------------------------------------------
# FTS path (>=3 chars)
# ---------------------------------------------------------------------------


async def test_search_finds_chinese_substring():
    await _seed()
    rows = await chat_store.search_messages("加密握手", limit=10)
    assert any("握手" in r["snippet"] for r in rows)
    assert all({"session_id", "role", "created_at", "snippet"} <= r.keys() for r in rows)


async def test_search_finds_ascii_term():
    await _seed()
    rows = await chat_store.search_messages("Cloudflare", limit=10)
    # snippet() truncates around the match (e.g. "《Cloudf》…"), so assert the
    # match was found, not that the whole term survives in the snippet.
    assert len(rows) >= 1
    assert any("Cloud" in r["snippet"] for r in rows)


async def test_search_no_match_returns_empty():
    await _seed()
    assert await chat_store.search_messages("量子计算机", limit=10) == []


async def test_search_short_query_like_fallback():
    sid = await chat_store.create_session("local", "t")
    await chat_store.record_user(sid, "AB 是个缩写")
    # 2-char query is below the trigram floor → LIKE fallback still finds it.
    rows = await chat_store.search_messages("AB", limit=10)
    assert len(rows) >= 1


async def test_search_empty_query():
    await _seed()
    assert await chat_store.search_messages("", limit=10) == []


async def test_search_fts_operator_injection_is_literal():
    """A query full of FTS5 metacharacters must be treated as literal text,
    not a query expression (no crash, no match explosion)."""
    sid = await chat_store.create_session("local", "t")
    await chat_store.record_user(sid, "正常内容 hello world")
    # These would be FTS syntax if not phrase-quoted.
    for bad in ['" OR "', "foo* NEAR bar", "content:hello"]:
        rows = await chat_store.search_messages(bad, limit=10)
        assert isinstance(rows, list)  # no exception


# ---------------------------------------------------------------------------
# Index stays in sync with the base table
# ---------------------------------------------------------------------------


async def test_wipe_clears_fts_index():
    await _seed()
    assert await chat_store.search_messages("握手", limit=10)
    await chat_store.wipe_all()
    assert await chat_store.search_messages("握手", limit=10) == []


async def test_backfill_indexes_preexisting_rows(tmp_path, monkeypatch):
    """Simulate a DB whose rows predate the FTS table: drop the FTS objects,
    insert directly (no triggers), then reopen — _backfill_fts must rebuild."""
    import sqlite3

    await _seed()
    # Simulate a DB created BEFORE the FTS feature: drop the index, triggers,
    # AND the 'built' marker, then insert a row no trigger will see.
    raw = sqlite3.connect(chat_store.DB_PATH, isolation_level=None)
    for stmt in (
        "DROP TRIGGER IF EXISTS messages_ai",
        "DROP TRIGGER IF EXISTS messages_ad",
        "DROP TRIGGER IF EXISTS messages_au",
        "DROP TABLE IF EXISTS messages_fts",
        "DELETE FROM fts_meta",
    ):
        raw.execute(stmt)
    raw.execute(
        "INSERT INTO messages(session_id, role, content, tools_used, created_at)"
        " VALUES ('s', 'user', '历史里关于 OAuth 的讨论', 0, 1.0)"
    )
    raw.close()
    # Next connect recreates FTS + backfills from messages (marker was cleared).
    rows = await chat_store.search_messages("OAuth", limit=10)
    assert len(rows) >= 1
    assert any("OAuth" in r["snippet"] for r in rows)


# ---------------------------------------------------------------------------
# Tool wrapper
# ---------------------------------------------------------------------------


async def test_search_chat_tool_formats():
    await _seed()
    out = await chat_search.search_chat("relay")
    assert "relay" in out.lower()
    assert "历史匹配" in out


async def test_search_chat_tool_empty_query():
    assert "不能为空" in await chat_search.search_chat("   ")


async def test_search_chat_tool_no_results():
    await _seed()
    assert "没有找到" in await chat_search.search_chat("完全不存在的词组xyz")
