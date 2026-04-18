"""Smoke tests for chat_handler.handle_chat.

Arcana's runtime is stubbed. The stub session yields a configurable
sequence of fake events whose `.event_type` strings contain the
substrings handle_chat matches on (`LLM_CHUNK`, `TOOL_START`, etc).
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

import chat_handler


@dataclass
class FakeEvent:
    event_type: str
    content: str = ""
    tool_name: str | None = None


class StubSession:
    """Minimal Arcana ChatSession stand-in.

    `events` is a list yielded in order when handle_chat awaits .stream().
    """

    def __init__(self, events):
        self.events = events
        self.last_prompt: str | None = None

    def stream(self, user_text):
        self.last_prompt = user_text
        events = self.events

        async def gen():
            for e in events:
                yield e

        return gen()


def _collect_send():
    """Return (send_callback, captured_frames_list)."""
    frames: list[dict] = []

    async def send(frame):
        frames.append(frame)

    return send, frames


@pytest.fixture(autouse=True)
def _no_history_writes(monkeypatch):
    """Default: neutralize chat_store so tests that don't opt in don't hit disk.

    Individual tests re-monkeypatch with real capturing stubs as needed.
    """

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(chat_handler.chat_store, "record_user", noop)
    monkeypatch.setattr(chat_handler.chat_store, "record_assistant", noop)


async def test_empty_user_text_returns_without_emitting():
    session = StubSession(events=[])
    send, frames = _collect_send()

    result = await chat_handler.handle_chat(session, "   ", send)

    assert result == ("", 0)
    assert frames == []
    # Session.stream was never called
    assert session.last_prompt is None


async def test_basic_llm_chunks_emit_delta_and_done():
    session = StubSession(
        events=[
            FakeEvent("LLM_CHUNK", content="Hello "),
            FakeEvent("LLM_CHUNK", content="world"),
        ]
    )
    send, frames = _collect_send()

    full, tools = await chat_handler.handle_chat(session, "hi", send)

    assert full == "Hello world"
    assert tools == 0
    assert [f["type"] for f in frames] == ["thinking", "delta", "delta", "done"]
    assert frames[1]["text"] == "Hello "
    assert frames[2]["text"] == "world"
    assert frames[3]["content"] == "Hello world"
    assert frames[3]["tools_used"] == 0
    assert "sessions" not in frames[3]  # no tools used -> no iTerm list


async def test_tool_start_and_tool_end_frames(monkeypatch):
    # Tools used -> handle_chat tries to import iterm_bridge.bridge. Bypass
    # that by forcing include_sessions_on_done=False so the import is skipped.
    session = StubSession(
        events=[
            FakeEvent("TOOL_CALL_START", tool_name="shell"),
            FakeEvent("LLM_CHUNK", content="ok"),
            FakeEvent("TOOL_RESULT", tool_name="shell"),
        ]
    )
    send, frames = _collect_send()

    full, tools = await chat_handler.handle_chat(
        session, "do thing", send, include_sessions_on_done=False
    )

    assert full == "ok"
    assert tools == 1
    types = [f["type"] for f in frames]
    assert types == ["thinking", "tool_start", "delta", "tool_end", "done"]
    assert frames[1]["name"] == "shell"
    assert frames[3]["name"] == "shell"
    assert frames[4]["tools_used"] == 1


async def test_tools_used_counts_all_tool_starts():
    session = StubSession(
        events=[
            FakeEvent("TOOL_START", tool_name="a"),
            FakeEvent("TOOL_END", tool_name="a"),
            FakeEvent("TOOL_START", tool_name="b"),
            FakeEvent("TOOL_END", tool_name="b"),
            FakeEvent("TOOL_START", tool_name="c"),
            FakeEvent("TOOL_END", tool_name="c"),
            FakeEvent("LLM_CHUNK", content="done"),
        ]
    )
    send, frames = _collect_send()

    _, tools = await chat_handler.handle_chat(
        session, "go", send, include_sessions_on_done=False
    )
    assert tools == 3
    assert frames[-1]["tools_used"] == 3


async def test_run_complete_fallback_when_no_chunks():
    # When the model produced no LLM_CHUNKs, handle_chat falls back to
    # the RUN_COMPLETE event's `.content`.
    session = StubSession(
        events=[
            FakeEvent("RUN_COMPLETE", content="final answer"),
        ]
    )
    send, frames = _collect_send()

    full, tools = await chat_handler.handle_chat(session, "hello", send)
    assert full == "final answer"
    assert tools == 0
    assert frames[-1]["content"] == "final answer"


async def test_history_session_id_records_user_and_assistant(monkeypatch):
    """history_session_id != None -> record_user (before stream) and
    record_assistant (after stream) get called with the right args."""
    calls: list[tuple] = []

    async def fake_record_user(sid, content):
        calls.append(("user", sid, content))

    async def fake_record_assistant(sid, content, tools_used=0):
        calls.append(("assistant", sid, content, tools_used))

    monkeypatch.setattr(chat_handler.chat_store, "record_user", fake_record_user)
    monkeypatch.setattr(
        chat_handler.chat_store, "record_assistant", fake_record_assistant
    )

    session = StubSession(
        events=[
            FakeEvent("LLM_CHUNK", content="hi"),
            FakeEvent("TOOL_START", tool_name="x"),
            FakeEvent("TOOL_END", tool_name="x"),
        ]
    )
    send, _frames = _collect_send()

    await chat_handler.handle_chat(
        session,
        "hello",
        send,
        include_sessions_on_done=False,
        history_session_id="sess-123",
    )

    assert calls == [
        ("user", "sess-123", "hello"),
        ("assistant", "sess-123", "hi", 1),
    ]


async def test_no_history_session_id_skips_persistence(monkeypatch):
    """history_session_id=None -> neither record_* is called."""

    async def boom_user(*a, **k):
        raise AssertionError("record_user should not be called")

    async def boom_assistant(*a, **k):
        raise AssertionError("record_assistant should not be called")

    monkeypatch.setattr(chat_handler.chat_store, "record_user", boom_user)
    monkeypatch.setattr(chat_handler.chat_store, "record_assistant", boom_assistant)

    session = StubSession(events=[FakeEvent("LLM_CHUNK", content="k")])
    send, _ = _collect_send()

    await chat_handler.handle_chat(session, "hi", send)


async def test_include_sessions_on_done_false_skips_bridge(monkeypatch):
    """With tools_used > 0 but include_sessions_on_done=False, handle_chat
    must NOT import iterm_bridge. Fake the import to raise if touched."""

    import sys

    class BoomModule:
        def __getattr__(self, _name):
            raise AssertionError("iterm_bridge should not be imported")

    monkeypatch.setitem(sys.modules, "iterm_bridge", BoomModule())

    session = StubSession(
        events=[
            FakeEvent("TOOL_START", tool_name="t"),
            FakeEvent("TOOL_END", tool_name="t"),
            FakeEvent("LLM_CHUNK", content="done"),
        ]
    )
    send, frames = _collect_send()

    await chat_handler.handle_chat(
        session, "hi", send, include_sessions_on_done=False
    )

    # done frame exists, no 'sessions' key
    assert frames[-1]["type"] == "done"
    assert "sessions" not in frames[-1]


async def test_include_sessions_on_done_true_queries_bridge(monkeypatch):
    """With tools_used > 0 and include_sessions_on_done=True, handle_chat
    imports iterm_bridge.bridge and attaches `sessions` to the done frame."""

    import sys
    import types

    @dataclass
    class FakeSessRow:
        session_id: str
        project: str
        name: str

    class FakeBridge:
        async def list_sessions(self):
            return [FakeSessRow("id1", "proj1", "name1")]

    module = types.ModuleType("iterm_bridge")
    module.bridge = FakeBridge()
    monkeypatch.setitem(sys.modules, "iterm_bridge", module)

    session = StubSession(
        events=[
            FakeEvent("TOOL_START", tool_name="t"),
            FakeEvent("TOOL_END", tool_name="t"),
            FakeEvent("LLM_CHUNK", content="ok"),
        ]
    )
    send, frames = _collect_send()

    await chat_handler.handle_chat(
        session, "hi", send, include_sessions_on_done=True
    )

    done = frames[-1]
    assert done["type"] == "done"
    assert done["sessions"] == [{"id": "id1", "project": "proj1", "name": "name1"}]


async def test_bridge_failure_is_swallowed(monkeypatch):
    """If iterm_bridge.bridge.list_sessions raises, the done frame is still
    sent without `sessions` and no exception escapes handle_chat."""

    import sys
    import types

    class FakeBridge:
        async def list_sessions(self):
            raise RuntimeError("iterm down")

    module = types.ModuleType("iterm_bridge")
    module.bridge = FakeBridge()
    monkeypatch.setitem(sys.modules, "iterm_bridge", module)

    session = StubSession(
        events=[
            FakeEvent("TOOL_START", tool_name="t"),
            FakeEvent("TOOL_END", tool_name="t"),
            FakeEvent("LLM_CHUNK", content="ok"),
        ]
    )
    send, frames = _collect_send()

    await chat_handler.handle_chat(session, "hi", send, include_sessions_on_done=True)
    assert frames[-1]["type"] == "done"
    assert "sessions" not in frames[-1]
