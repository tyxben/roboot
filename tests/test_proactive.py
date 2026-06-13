"""Tests for proactive.py — daily-briefing scheduling + headless brief turn."""

from __future__ import annotations

import asyncio
import contextlib
import datetime

import pytest

import proactive
import tool_guard


# -----------------------------------------------------------------------------
# seconds_until_daily (pure)
# -----------------------------------------------------------------------------


def test_seconds_until_later_today():
    now = datetime.datetime(2026, 6, 13, 7, 0, 0)
    assert proactive.seconds_until_daily("08:00", now) == 3600.0


def test_seconds_until_wraps_to_tomorrow():
    now = datetime.datetime(2026, 6, 13, 9, 0, 0)
    # next 08:00 is 23h out
    assert proactive.seconds_until_daily("08:00", now) == 23 * 3600.0


def test_seconds_until_exact_now_is_next_day():
    now = datetime.datetime(2026, 6, 13, 8, 0, 0)
    # target == now → schedule for tomorrow, never a busy-loop on the same minute
    assert proactive.seconds_until_daily("08:00", now) == 86400.0


@pytest.mark.parametrize("bad", ["", "8", "8:00:00", "25:00", "08:99", "ab:cd", "-1:00"])
def test_seconds_until_rejects_malformed(bad):
    with pytest.raises(ValueError):
        proactive.seconds_until_daily(bad, datetime.datetime(2026, 6, 13, 7, 0, 0))


# -----------------------------------------------------------------------------
# run_briefing_once
# -----------------------------------------------------------------------------


async def test_run_briefing_once_pushes_single_response(monkeypatch):
    """The brief streams to an internal sink and pushes the RESULT as ONE
    self-contained `response` frame — never per-delta (which would clobber an
    in-flight user turn / reorder on the relay)."""
    pushed: list[dict] = []

    async def fake_push(frame):
        pushed.append(frame)

    class _FakeSession:
        pass

    class _FakeRuntime:
        def create_chat_session(self, system_prompt):
            assert system_prompt == "PERSONALITY"  # build_personality() output
            return _FakeSession()

    async def fake_handle_chat(session, prompt, send, *, history_session_id=None):
        assert isinstance(session, _FakeSession)
        assert prompt == "BRIEF_PROMPT"
        assert history_session_id is None  # brief is not persisted as user chat
        await send({"type": "delta", "text": "x"})  # swallowed by the sink
        return ("> 总结\n今日要点", 1)

    import chat_handler

    monkeypatch.setattr(chat_handler, "handle_chat", fake_handle_chat)
    text = await proactive.run_briefing_once(
        _FakeRuntime(), "BRIEF_PROMPT", fake_push, build_personality=lambda: "PERSONALITY"
    )
    assert "总结" in text
    assert len(pushed) == 1  # exactly one frame, not the streamed deltas
    assert pushed[0]["type"] == "response"
    assert pushed[0]["kind"] == "briefing"
    assert "今日要点" in pushed[0]["content"]


async def test_run_briefing_once_runs_under_autonomous_origin(monkeypatch):
    """The brief turn must run under the 'briefing' autonomous origin so the
    gate forces it read-only; the origin is restored afterwards."""
    seen = {}

    async def fake_push(frame):
        pass

    class _R:
        def create_chat_session(self, system_prompt):
            return object()

    async def fake_handle_chat(session, prompt, send, *, history_session_id=None):
        seen["origin"] = tool_guard.current_origin.get()
        return ("brief", 0)

    import chat_handler

    monkeypatch.setattr(chat_handler, "handle_chat", fake_handle_chat)
    await proactive.run_briefing_once(_R(), "p", fake_push, build_personality=lambda: "x")
    assert seen["origin"] == "briefing"
    assert tool_guard.current_origin.get() == "local"  # restored (default)


async def test_run_briefing_once_uses_in_flight(monkeypatch):
    """The optional in_flight CM wraps the turn (server uses it to defer a
    self-upgrade re-exec until the brief finishes)."""
    events: list[str] = []

    @contextlib.asynccontextmanager
    async def fake_inflight():
        events.append("enter")
        try:
            yield
        finally:
            events.append("exit")

    async def fake_push(frame):
        pass

    class _R:
        def create_chat_session(self, system_prompt):
            return object()

    async def fake_handle_chat(*a, **kw):
        events.append("turn")
        return ("brief", 0)

    import chat_handler

    monkeypatch.setattr(chat_handler, "handle_chat", fake_handle_chat)
    await proactive.run_briefing_once(
        _R(), "p", fake_push, build_personality=lambda: "x", in_flight=fake_inflight
    )
    assert events == ["enter", "turn", "exit"]


async def test_run_briefing_once_swallows_errors(monkeypatch):
    async def fake_send(frame):
        pass

    class _BoomRuntime:
        def create_chat_session(self, system_prompt):
            raise RuntimeError("session boom")

    # A failing brief must return "" (never raise) so it can't kill the loop.
    text = await proactive.run_briefing_once(
        _BoomRuntime(), "P", fake_send, build_personality=lambda: "X"
    )
    assert text == ""


# -----------------------------------------------------------------------------
# briefing_loop
# -----------------------------------------------------------------------------


async def test_briefing_loop_invalid_time_returns_immediately():
    # An invalid time disables the loop (logs + returns) — no infinite spin.
    await asyncio.wait_for(
        proactive.briefing_loop(
            object(), hhmm="nope", prompt="p", push=None, build_personality=lambda: "x"
        ),
        timeout=1.0,
    )


async def test_briefing_loop_fires_then_reschedules(monkeypatch):
    runs = {"n": 0}
    sleeps: list[float] = []

    monkeypatch.setattr(proactive, "seconds_until_daily", lambda hhmm, now=None: 0.0)

    async def fake_sleep(s):
        sleeps.append(s)
        if len(sleeps) >= 3:  # let it fire ~once, then break out
            raise asyncio.CancelledError()

    async def fake_run(*a, **kw):
        runs["n"] += 1
        return "brief"

    monkeypatch.setattr(proactive.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(proactive, "run_briefing_once", fake_run)

    with pytest.raises(asyncio.CancelledError):
        await proactive.briefing_loop(
            object(), hhmm="08:00", prompt="p", push=None, build_personality=lambda: "x"
        )
    assert runs["n"] >= 1  # the brief fired at least once
