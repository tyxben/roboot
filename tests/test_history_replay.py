"""Tests for memory.py -- Layer A (replay) + Layer B (distill counter).

Both layers are tested with fakes: a stub ChatSession whose `_messages` is
just a plain list, and a monkeypatched chat_store that returns pre-baked
transcripts without touching SQLite.
"""

from __future__ import annotations

import asyncio

import pytest

import memory


# -----------------------------------------------------------------------------
# Fakes
# -----------------------------------------------------------------------------


class FakeChatSession:
    """Mimics Arcana's ChatSession surface that memory.py relies on.

    The real class exposes `_messages: list[Message]` (starts with a system
    prompt). We seed with a sentinel so tests can assert the replay
    appended rather than replacing.
    """

    def __init__(self, system_prompt: str = "sys"):
        from arcana.runtime.conversation import Message, MessageRole

        self._messages = [Message(role=MessageRole.SYSTEM, content=system_prompt)]


# -----------------------------------------------------------------------------
# Layer A -- replay_history
# -----------------------------------------------------------------------------


async def test_replay_history_seeds_last_n_in_order(monkeypatch):
    """chat_store returns oldest-first; memory must seed them in the same
    order, preserving roles, after the system prompt."""
    history = [
        {"role": "user", "content": "msg 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "msg 2"},
        {"role": "assistant", "content": "reply 2"},
    ]

    async def fake_list(sid, limit=200):
        assert sid == "s-abc"
        assert limit == 10  # default N
        return history

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    session = FakeChatSession()
    seeded = await memory.replay_history(session, "s-abc")

    assert seeded == 4
    # System prompt still first, then the 4 replayed turns.
    roles = [str(m.role.value) for m in session._messages]
    contents = [m.content for m in session._messages]
    assert roles == ["system", "user", "assistant", "user", "assistant"]
    assert contents == ["sys", "msg 1", "reply 1", "msg 2", "reply 2"]


async def test_replay_history_noop_on_empty(monkeypatch):
    async def fake_list(sid, limit=200):
        return []

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    session = FakeChatSession()
    before = list(session._messages)
    seeded = await memory.replay_history(session, "anything")
    assert seeded == 0
    assert session._messages == before


async def test_replay_history_noop_when_no_session_id(monkeypatch):
    """Called with falsy history_session_id -> must not even hit chat_store."""

    async def boom(*a, **kw):
        raise AssertionError("chat_store.list_messages must not be called")

    monkeypatch.setattr(memory.chat_store, "list_messages", boom)

    session = FakeChatSession()
    seeded = await memory.replay_history(session, None)
    assert seeded == 0
    seeded = await memory.replay_history(session, "")
    assert seeded == 0


async def test_replay_history_skips_unknown_roles(monkeypatch):
    """Tool-role messages (or weird rows) get skipped, not crashed on."""
    history = [
        {"role": "user", "content": "hi"},
        {"role": "tool", "content": "tool output"},  # unknown -> skip
        {"role": "assistant", "content": "bye"},
        {"role": "user", "content": ""},  # empty content -> skip
    ]

    async def fake_list(sid, limit=200):
        return history

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    session = FakeChatSession()
    seeded = await memory.replay_history(session, "sid")
    assert seeded == 2  # only the user+assistant with non-empty content

    contents = [m.content for m in session._messages]
    assert contents == ["sys", "hi", "bye"]


async def test_replay_history_respects_limit_argument(monkeypatch):
    """`n` is passed through to chat_store.list_messages as `limit`."""
    captured: dict = {}

    async def fake_list(sid, limit=200):
        captured["limit"] = limit
        return []

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    session = FakeChatSession()
    await memory.replay_history(session, "sid", n=25)
    assert captured["limit"] == 25


async def test_replay_history_chat_store_failure_is_swallowed(monkeypatch):
    """If chat_store raises, replay_history logs + returns 0, never raises."""

    async def boom(sid, limit=200):
        raise RuntimeError("db gone")

    monkeypatch.setattr(memory.chat_store, "list_messages", boom)

    session = FakeChatSession()
    seeded = await memory.replay_history(session, "sid")
    assert seeded == 0
    # System prompt untouched
    assert len(session._messages) == 1


async def test_replay_history_fallback_when_no_backing_list(monkeypatch):
    """If the session object exposes no reachable message list, memory
    falls back to appending a synthetic context summary to `_messages`
    if one exists; otherwise 0. Exercises the fallback path explicitly."""

    class SessionWithoutBackingList:
        # No _messages, no messages attribute.
        pass

    async def fake_list(sid, limit=200):
        return [{"role": "user", "content": "hi"}]

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    session = SessionWithoutBackingList()
    seeded = await memory.replay_history(session, "sid")
    # No backing list anywhere → returns 0 rather than exploding.
    assert seeded == 0


# -----------------------------------------------------------------------------
# Layer B -- TurnCounter
# -----------------------------------------------------------------------------


def test_turn_counter_fires_on_k():
    c = memory.TurnCounter(every_k=3)
    assert not c.should_distill("u1")
    c.bump("u1")
    assert not c.should_distill("u1")
    c.bump("u1")
    assert not c.should_distill("u1")
    c.bump("u1")
    assert c.should_distill("u1")  # hits K
    c.reset("u1")
    assert not c.should_distill("u1")
    assert c.get("u1") == 0


def test_turn_counter_independent_per_key():
    c = memory.TurnCounter(every_k=2)
    c.bump("a")
    c.bump("a")
    c.bump("b")
    assert c.should_distill("a")
    assert not c.should_distill("b")


async def test_record_turn_schedules_distill_at_k(monkeypatch):
    """After K bumps the helper schedules a distillation task and resets."""
    k = 3
    scheduled: list = []

    async def fake_distill(sid, *, runtime, k=None):
        scheduled.append((sid, k))
        return None

    monkeypatch.setattr(memory, "distill_and_record", fake_distill)

    class StubRuntime:
        pass

    runtime = StubRuntime()
    c = memory.TurnCounter(every_k=k)

    task1 = memory.record_turn_and_maybe_distill(
        "s1", runtime=runtime, counter=c
    )
    task2 = memory.record_turn_and_maybe_distill(
        "s1", runtime=runtime, counter=c
    )
    assert task1 is None and task2 is None
    assert c.get("s1") == 2

    task3 = memory.record_turn_and_maybe_distill(
        "s1", runtime=runtime, counter=c
    )
    assert task3 is not None
    # Counter resets after scheduling so the next K turns aren't pre-counted.
    assert c.get("s1") == 0

    await task3  # let the fake distill coroutine finish.
    assert scheduled == [("s1", k)]


async def test_record_turn_noop_on_empty_history_id(monkeypatch):
    """Without a history_session_id we can't address a transcript; skip."""

    async def boom(*a, **kw):
        raise AssertionError("distill must not run without history_id")

    monkeypatch.setattr(memory, "distill_and_record", boom)

    c = memory.TurnCounter(every_k=1)
    task = memory.record_turn_and_maybe_distill(None, runtime=object(), counter=c)
    assert task is None
    task = memory.record_turn_and_maybe_distill("", runtime=object(), counter=c)
    assert task is None


# -----------------------------------------------------------------------------
# Layer B -- distill_and_record
# -----------------------------------------------------------------------------


async def test_distill_records_non_trivial_output(monkeypatch):
    recorded: list[str] = []

    async def fake_list(sid, limit=200):
        return [
            {"role": "user", "content": "我叫 Ty，正在做 Roboot"},
            {"role": "assistant", "content": "好的"},
        ]

    async def fake_runner(system_prompt, user_text):
        # Returns a non-trivial delta — should hit remember_user.
        return "用户叫 Ty，正在构建一个叫 Roboot 的个人 AI 助手。"

    async def fake_remember(fact):
        recorded.append(fact)
        return "记住了"

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)

    result = await memory.distill_and_record(
        "s1",
        runtime=object(),
        runner=fake_runner,
        remember_user_fn=fake_remember,
        k=5,
    )
    assert result is not None
    assert "Ty" in result
    assert recorded == [result]


async def test_distill_ignores_nothing_sentinel(monkeypatch):
    calls: list = []

    async def fake_list(sid, limit=200):
        return [{"role": "user", "content": "hi"}]

    async def fake_runner(sp, ut):
        return "NOTHING"

    async def fake_remember(fact):
        calls.append(fact)
        return "x"

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)
    result = await memory.distill_and_record(
        "s1",
        runtime=object(),
        runner=fake_runner,
        remember_user_fn=fake_remember,
    )
    assert result is None
    assert calls == []


async def test_distill_ignores_too_short_output(monkeypatch):
    """Outputs below DISTILL_MIN_LEN are treated as no-op."""
    calls: list = []

    async def fake_list(sid, limit=200):
        return [{"role": "user", "content": "hi"}]

    async def fake_runner(sp, ut):
        return "ok"  # 2 chars << DISTILL_MIN_LEN

    async def fake_remember(fact):
        calls.append(fact)
        return "x"

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)
    result = await memory.distill_and_record(
        "s1",
        runtime=object(),
        runner=fake_runner,
        remember_user_fn=fake_remember,
    )
    assert result is None
    assert calls == []


async def test_distill_swallows_runner_failure(monkeypatch):
    async def fake_list(sid, limit=200):
        return [{"role": "user", "content": "hi"}]

    async def fake_runner(sp, ut):
        raise RuntimeError("LLM fell over")

    async def fake_remember(fact):
        raise AssertionError("remember_user must not run if runner failed")

    monkeypatch.setattr(memory.chat_store, "list_messages", fake_list)
    result = await memory.distill_and_record(
        "s1",
        runtime=object(),
        runner=fake_runner,
        remember_user_fn=fake_remember,
    )
    assert result is None
