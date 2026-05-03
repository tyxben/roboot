"""Tests for the proactive session-waiting notifier.

The watcher's idle <-> waiting state machine is the only thing worth
exercising deterministically. We stub `iterm_bridge` in sys.modules so
poll_once() has no dependency on a real iTerm2 install.
"""

from __future__ import annotations

import sys
import types
from dataclasses import dataclass

import pytest

from session_watcher import (
    TAIL_LINES,
    SessionWatcher,
    _match_prompt_line,
    _sanitize_prompt_line,
)


@dataclass
class FakeSess:
    session_id: str
    project: str


class FakeBridge:
    """Drop-in replacement for iterm_bridge.bridge.

    `sessions` stays constant across polls; `tails[session_id]` is swapped
    out between polls to drive the watcher's state machine.
    """

    def __init__(self, sessions: list[FakeSess], tails: dict[str, str]):
        self.sessions = sessions
        self.tails = tails

    async def list_sessions(self):
        return list(self.sessions)

    async def read_session(self, session_id: str, num_lines: int = 20):
        return self.tails.get(session_id, "")


@pytest.fixture
def fake_bridge(monkeypatch):
    """Install a mutable FakeBridge under `iterm_bridge.bridge`."""
    sessions = [FakeSess(session_id="sess-1", project="roboot")]
    tails = {"sess-1": ""}
    bridge = FakeBridge(sessions, tails)

    module = types.ModuleType("iterm_bridge")
    module.bridge = bridge
    monkeypatch.setitem(sys.modules, "iterm_bridge", module)
    return bridge


def _collect_subscriber():
    calls: list[dict] = []

    async def cb(payload: dict):
        calls.append(payload)

    return cb, calls


def test_match_prompt_line_picks_most_recent():
    tail = "older\nSome line\n\nDo you want to proceed?\nlast"
    got = _match_prompt_line(tail)
    assert got == "Do you want to proceed?"


def test_match_prompt_line_returns_none_when_no_hit():
    assert _match_prompt_line("just some output\nnothing here") is None


async def test_fires_once_on_idle_to_waiting(fake_bridge):
    watcher = SessionWatcher()
    cb, calls = _collect_subscriber()
    watcher.subscribe(cb)

    # Poll 1: idle (no prompt). No notification.
    fake_bridge.tails["sess-1"] = "some output\n$ "
    await watcher.poll_once()
    assert calls == []

    # Poll 2: waiting (prompt appears). Fires once.
    fake_bridge.tails["sess-1"] = "build output\nDo you want to proceed? [y/n]"
    await watcher.poll_once()
    assert len(calls) == 1
    assert calls[0]["session_id"] == "sess-1"
    assert calls[0]["project"] == "roboot"
    assert "Do you want to proceed" in calls[0]["prompt_line"]

    # Poll 3: still waiting (same prompt). MUST NOT refire.
    await watcher.poll_once()
    assert len(calls) == 1

    # Poll 4: still waiting with slightly different tail context, same
    # prompt still visible. Still must not refire.
    fake_bridge.tails["sess-1"] = "newer chatter\nDo you want to proceed? [y/n]"
    await watcher.poll_once()
    assert len(calls) == 1


async def test_refires_after_prompt_disappears_then_reappears(fake_bridge):
    watcher = SessionWatcher()
    cb, calls = _collect_subscriber()
    watcher.subscribe(cb)

    fake_bridge.tails["sess-1"] = "Do you want to proceed?"
    await watcher.poll_once()
    assert len(calls) == 1

    # Prompt answered / gone -> state flips back to idle.
    fake_bridge.tails["sess-1"] = "ok, done.\n$ "
    await watcher.poll_once()
    assert len(calls) == 1  # no notification on waiting -> idle

    # Prompt reappears -> fires again.
    fake_bridge.tails["sess-1"] = "Do you want to proceed?"
    await watcher.poll_once()
    assert len(calls) == 2


async def test_bridge_exceptions_do_not_crash_loop(monkeypatch):
    """If list_sessions raises, poll_once swallows it silently."""

    class ExplodingBridge:
        async def list_sessions(self):
            raise RuntimeError("iterm2 down")

        async def read_session(self, *a, **k):
            return ""

    module = types.ModuleType("iterm_bridge")
    module.bridge = ExplodingBridge()
    monkeypatch.setitem(sys.modules, "iterm_bridge", module)

    watcher = SessionWatcher()
    cb, calls = _collect_subscriber()
    watcher.subscribe(cb)

    # Does not raise.
    await watcher.poll_once()
    assert calls == []


async def test_subscriber_failure_does_not_block_others(fake_bridge):
    watcher = SessionWatcher()

    async def boom(_payload):
        raise RuntimeError("nope")

    good_calls: list[dict] = []

    async def good(payload):
        good_calls.append(payload)

    watcher.subscribe(boom)
    watcher.subscribe(good)

    fake_bridge.tails["sess-1"] = "Do you want to proceed?"
    await watcher.poll_once()
    assert len(good_calls) == 1


async def test_stale_sessions_get_forgotten(fake_bridge):
    """If a session disappears, its state entry is cleaned up so a new
    session reusing the id starts fresh."""
    watcher = SessionWatcher()
    cb, calls = _collect_subscriber()
    watcher.subscribe(cb)

    fake_bridge.tails["sess-1"] = "Do you want to proceed?"
    await watcher.poll_once()
    assert len(calls) == 1
    assert "sess-1" in watcher._states

    # Session disappears.
    fake_bridge.sessions.clear()
    await watcher.poll_once()
    assert "sess-1" not in watcher._states


def test_tail_lines_is_ten():
    """Tail window matches the spec — 10 is enough for the prompt regex
    and keeps the per-poll footprint small across many sessions."""
    assert TAIL_LINES == 10


def test_sanitize_prompt_line_strips_ansi_escapes_html_and_controls():
    raw = "\x1b[31mHello <script>alert(1)</script>\x1b[0m"
    assert (
        _sanitize_prompt_line(raw)
        == "Hello &lt;script&gt;alert(1)&lt;/script&gt;"
    )

    # Control characters (other than \t) are stripped.
    assert _sanitize_prompt_line("a\x00b\x07c\x1fd") == "abcd"

    # Tabs and runs of whitespace collapse to single spaces.
    assert _sanitize_prompt_line("  foo\t\tbar\n\nbaz  ") == "foo bar baz"

    # Ampersand is escaped too (not just < and >).
    assert _sanitize_prompt_line("A & B") == "A &amp; B"

    # Empty / None-ish inputs are safe.
    assert _sanitize_prompt_line("") == ""


def test_sanitize_prompt_line_truncates_and_is_idempotent():
    # Truncation: 201 A's -> 200 A's + ellipsis.
    long = "A" * 300
    got = _sanitize_prompt_line(long)
    assert got == ("A" * 200) + "…"
    assert len(got) == 201  # 200 chars + single-char ellipsis

    # Idempotent: running sanitize twice yields the same result.
    raw = "\x1b[31m<b>hi</b>\x1b[0m\t\ta & b"
    once = _sanitize_prompt_line(raw)
    twice = _sanitize_prompt_line(once)
    assert once == twice


def _collect_sessions_changed_subscriber():
    calls: list[None] = []

    async def cb():
        calls.append(None)

    return cb, calls


async def test_sessions_changed_fires_on_add_and_remove(fake_bridge):
    """The diff must broadcast on both add and remove, and the very first
    poll must NOT broadcast (initial seed, not a change)."""
    watcher = SessionWatcher()
    cb, calls = _collect_sessions_changed_subscriber()
    watcher.subscribe_sessions_changed(cb)

    # Poll 1: [A, B] -> initial seed, NO broadcast.
    fake_bridge.sessions[:] = [
        FakeSess(session_id="A", project="p"),
        FakeSess(session_id="B", project="p"),
    ]
    await watcher.poll_once()
    assert calls == []

    # Poll 2: [A, B, C] -> add, fires.
    fake_bridge.sessions.append(FakeSess(session_id="C", project="p"))
    await watcher.poll_once()
    assert len(calls) == 1

    # Poll 3: [A, B, C] unchanged -> no fire.
    await watcher.poll_once()
    assert len(calls) == 1

    # Poll 4: [A] -> remove, fires.
    fake_bridge.sessions[:] = [FakeSess(session_id="A", project="p")]
    await watcher.poll_once()
    assert len(calls) == 2


async def test_sessions_changed_silent_after_bridge_exception(monkeypatch):
    """If list_sessions raises, the diff state must NOT advance — otherwise
    a transient iTerm hiccup followed by recovery would look like every
    session reappeared and fire a spurious broadcast."""

    class FlakyBridge:
        def __init__(self):
            self.sessions = [
                FakeSess(session_id="A", project="p"),
                FakeSess(session_id="B", project="p"),
            ]
            self._call = 0

        async def list_sessions(self):
            self._call += 1
            if self._call == 2:
                raise RuntimeError("iterm2 hiccup")
            return list(self.sessions)

        async def read_session(self, *a, **k):
            return ""

    bridge = FlakyBridge()
    module = types.ModuleType("iterm_bridge")
    module.bridge = bridge
    monkeypatch.setitem(sys.modules, "iterm_bridge", module)

    watcher = SessionWatcher()
    cb, calls = _collect_sessions_changed_subscriber()
    watcher.subscribe_sessions_changed(cb)

    # Poll 1: success, seed silently.
    await watcher.poll_once()
    assert calls == []
    assert watcher._prev_session_ids == {"A", "B"}

    # Poll 2: bridge raises. State must NOT advance and callback must not fire.
    await watcher.poll_once()
    assert calls == []
    assert watcher._prev_session_ids == {"A", "B"}

    # Poll 3: bridge recovers with the same set — still no spurious diff.
    await watcher.poll_once()
    assert calls == []
    assert watcher._prev_session_ids == {"A", "B"}


async def test_sessions_changed_subscriber_failure_isolated(fake_bridge):
    """One throwing subscriber must not block the others."""
    watcher = SessionWatcher()

    async def boom():
        raise RuntimeError("nope")

    good_calls: list[None] = []

    async def good():
        good_calls.append(None)

    watcher.subscribe_sessions_changed(boom)
    watcher.subscribe_sessions_changed(good)

    # Seed.
    await watcher.poll_once()
    # Trigger a real change.
    fake_bridge.sessions.append(FakeSess(session_id="sess-2", project="p"))
    await watcher.poll_once()
    assert len(good_calls) == 1


async def test_notify_sanitizes_prompt_line_before_subscribers(fake_bridge):
    """Subscribers must receive a sanitized prompt_line, not the raw
    terminal bytes — prompt-injection + XSS defense-in-depth."""
    watcher = SessionWatcher()
    cb, calls = _collect_subscriber()
    watcher.subscribe(cb)

    # ANSI-wrapped prompt with HTML-ish payload embedded in the matched line.
    fake_bridge.tails["sess-1"] = (
        "warm up\n"
        "\x1b[31m<script>x</script> Do you want to proceed? [y/n]\x1b[0m"
    )
    await watcher.poll_once()

    assert len(calls) == 1
    line = calls[0]["prompt_line"]
    # No raw ANSI escape, no raw angle brackets.
    assert "\x1b" not in line
    assert "<script>" not in line
    assert "&lt;script&gt;" in line
    assert "Do you want to proceed" in line
