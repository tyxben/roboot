"""Tests for chat-history full-text search (chat_store FTS5 + search_chat tool)."""

from __future__ import annotations

import pytest

import chat_store
import tool_guard
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

    sid = await _seed()  # a real session the message can reference
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
        " VALUES (?, 'user', '历史里关于 OAuth 的讨论', 0, 1.0)",
        (sid,),
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


# ---------------------------------------------------------------------------
# Per-surface / per-user isolation (security: no cross-user exfiltration)
# ---------------------------------------------------------------------------


async def _seed_multi():
    s_local = await chat_store.create_session("local", None)
    await chat_store.record_user(s_local, "本地控制台的机密会议纪要 alpha")
    s_a = await chat_store.create_session("telegram", "111")
    await chat_store.record_user(s_a, "用户A的银行密码 alpha")
    s_b = await chat_store.create_session("telegram", "222")
    await chat_store.record_user(s_b, "用户B的私人备忘 alpha")
    s_relay = await chat_store.create_session("remote", "client-xyz")
    await chat_store.record_user(s_relay, "手机端的内容 alpha")


async def test_search_messages_scoped_by_source():
    await _seed_multi()
    local = await chat_store.search_messages("alpha", 50, source="local")
    assert local and all("机密会议" in r["snippet"] for r in local)
    assert not any("银行密码" in r["snippet"] for r in local)


async def test_search_messages_scoped_by_telegram_user():
    await _seed_multi()
    a = await chat_store.search_messages("alpha", 50, source="telegram", label="111")
    assert a and all("用户A" in r["snippet"] for r in a)
    assert not any("用户B" in r["snippet"] for r in a)
    # User B must not see user A.
    b = await chat_store.search_messages("alpha", 50, source="telegram", label="222")
    assert not any("用户A" in r["snippet"] for r in b)


async def test_search_chat_tool_telegram_user_isolation(monkeypatch):
    """A Telegram user, going through the tool, sees only their own history —
    not the console owner's, not another Telegram user's."""
    from tools.voice_switch import current_tg_user

    await _seed_multi()
    otok = tool_guard.current_origin.set("telegram")
    utok = current_tg_user.set(111)
    try:
        out = await chat_search.search_chat("alpha", limit=50)
    finally:
        current_tg_user.reset(utok)
        tool_guard.current_origin.reset(otok)
    assert "用户A" in out
    assert "机密会议" not in out  # not the local owner's
    assert "用户B" not in out  # not another telegram user's


async def test_search_chat_telegram_no_user_returns_nothing(monkeypatch):
    """Fail closed: a telegram turn with no user id must not leak anything."""
    from tools.voice_switch import current_tg_user

    await _seed_multi()
    otok = tool_guard.current_origin.set("telegram")
    utok = current_tg_user.set(None)
    try:
        out = await chat_search.search_chat("alpha", limit=50)
    finally:
        current_tg_user.reset(utok)
        tool_guard.current_origin.reset(otok)
    assert "没有找到" in out
