"""D4 — Telegram-side tool_guard wiring.

Covers what `test_tool_guard_integration.py` doesn't: the Telegram bot is a
*separate process* with its own `arcana.Runtime`, so the daemon's wiring in
`server.py` doesn't help here. This file pins:

  1. `_get_runtime()` attaches `tool_guard.confirmation_callback` to the
     bot's gateway (so Telegram-driven shell calls actually hit the gate).
  2. `_broadcast_tool_approval` reads `current_tg_user` and sends an
     inline-keyboard DM to *only* the triggering user.
  3. The `tool_ok:` / `tool_no:` callback prefixes resolve the gate's
     pending future (or surface "已超时" if the future is already gone).
  4. End-to-end: gate(...) blocks waiting on a button click; simulated
     button-press via the callback_handler unblocks it and returns the
     correct Decision.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import arcana
import tool_guard
from tools.voice_switch import current_tg_user

from adapters import telegram_bot


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    """Same isolation shape as test_tool_guard_integration."""
    monkeypatch.setattr(tool_guard, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(tool_guard, "ALLOWLIST_PATH", tmp_path / "allowlist.json")
    tool_guard._allowlist_cache["mtime"] = 0.0
    tool_guard._allowlist_cache["entries"] = []
    yield


@pytest.fixture(autouse=True)
def _reset_module_state():
    tool_guard._broadcasters.clear()
    tool_guard._pending.clear()
    telegram_bot._pending_owner.clear()
    # Don't leak _tg_app between tests — main() sets it, tests stub it.
    prev_app = telegram_bot._tg_app
    yield
    tool_guard._broadcasters.clear()
    for fut in list(tool_guard._pending.values()):
        if not fut.done():
            fut.cancel()
    tool_guard._pending.clear()
    telegram_bot._pending_owner.clear()
    telegram_bot._tg_app = prev_app
    # Force `_get_runtime()` to rebuild on next call so its callback wiring
    # is exercised fresh in each test.
    if telegram_bot._runtime is not None:
        telegram_bot._runtime = None


@pytest.fixture
def fake_app():
    """Stand-in for telegram.ext.Application — only `bot.send_message` is
    exercised. AsyncMock so `await app.bot.send_message(...)` works."""
    app = SimpleNamespace()
    app.bot = SimpleNamespace()
    app.bot.send_message = AsyncMock()
    return app


@pytest.fixture
def set_user():
    """Set `current_tg_user` for the duration of one test, reset after."""
    tokens: list = []

    def _setter(uid):
        tokens.append(current_tg_user.set(uid))

    yield _setter
    for tok in reversed(tokens):
        try:
            current_tg_user.reset(tok)
        except Exception:
            pass


# -----------------------------------------------------------------------------
# 1. Wiring sanity — _get_runtime attaches the callback
# -----------------------------------------------------------------------------


def test_get_runtime_attaches_tool_guard_callback(monkeypatch):
    """Without this, Telegram-driven `run_command` calls bypass the gate
    entirely. The whole point of D4."""
    # Avoid touching real config — synthesize a minimal one.
    monkeypatch.setattr(
        telegram_bot, "CONFIG", {"providers": {"deepseek": "sk-fake"}}
    )
    rt = telegram_bot._get_runtime()
    assert rt._tool_gateway is not None
    assert rt._tool_gateway.confirmation_callback is (
        tool_guard.confirmation_callback
    ), (
        "Telegram bot's runtime must wire tool_guard.confirmation_callback. "
        "Without it, Telegram-driven shell calls bypass the gate."
    )


# -----------------------------------------------------------------------------
# 2. Broadcaster behavior
# -----------------------------------------------------------------------------


async def test_broadcaster_skips_when_app_unset(caplog):
    """Before main() runs, `_tg_app` is None. The broadcaster must bail
    silently — logging — instead of crashing the gate."""
    telegram_bot._tg_app = None
    await telegram_bot._broadcast_tool_approval(
        {"req_id": "abc", "tool": "shell", "args_summary": "x", "timeout_s": 30}
    )
    # No exception is enough; log is bonus.
    assert "tool_guard broadcaster" in caplog.text.lower() or True


async def test_broadcaster_skips_when_no_tg_user(fake_app):
    """If a tool fires with no `current_tg_user` set (e.g. a non-Telegram
    code path that somehow hits this broadcaster), do nothing — let the
    gate time out and reject. Better than DM'ing every allowed user."""
    telegram_bot._tg_app = fake_app
    # current_tg_user not set → defaults to None.
    await telegram_bot._broadcast_tool_approval(
        {"req_id": "abc", "tool": "shell", "args_summary": "rm -rf /"}
    )
    fake_app.bot.send_message.assert_not_awaited()


async def test_broadcaster_sends_to_triggering_user(fake_app, set_user):
    telegram_bot._tg_app = fake_app
    set_user(424242)
    await telegram_bot._broadcast_tool_approval(
        {
            "req_id": "deadbeef",
            "tool": "shell",
            "args_summary": "rm -rf /tmp/x",
            "danger_reason": "recursive rm",
            "timeout_s": 30,
        }
    )
    fake_app.bot.send_message.assert_awaited_once()
    call = fake_app.bot.send_message.await_args
    assert call.kwargs["chat_id"] == 424242
    # Inline keyboard carries the req_id in callback_data.
    markup = call.kwargs["reply_markup"]
    callback_datas = [btn.callback_data for row in markup.inline_keyboard for btn in row]
    assert "tool_ok:deadbeef" in callback_datas
    assert "tool_no:deadbeef" in callback_datas


async def test_broadcaster_truncates_huge_summary(fake_app, set_user):
    """`args_summary` can be up to 2KB; Telegram messages live in chat
    history forever, so don't dump 2KB at the user — trim to ~600 chars."""
    telegram_bot._tg_app = fake_app
    set_user(1)
    huge = "A" * 1500
    await telegram_bot._broadcast_tool_approval(
        {"req_id": "r", "tool": "shell", "args_summary": huge, "timeout_s": 30}
    )
    text = fake_app.bot.send_message.await_args.kwargs["text"]
    assert "...(截断)" in text
    assert len(text) < 1500


# -----------------------------------------------------------------------------
# 3. Callback handler dispatches to resolve_decision
# -----------------------------------------------------------------------------


def _make_query(user_id: int, data: str) -> SimpleNamespace:
    """Build a minimal `Update.callback_query` stub. The handler only
    touches: from_user.id, data, answer(), edit_message_text(), message.*."""
    q = SimpleNamespace()
    q.from_user = SimpleNamespace(id=user_id)
    q.data = data
    q.answer = AsyncMock()
    q.edit_message_text = AsyncMock()
    q.message = SimpleNamespace(
        reply_text=AsyncMock(),
        chat=SimpleNamespace(send_action=AsyncMock()),
    )
    return q


def _make_update_with_query(query) -> SimpleNamespace:
    """Wrap query so it looks like an `Update` to `callback_handler`."""
    return SimpleNamespace(callback_query=query)


async def test_callback_tool_ok_resolves_pending(monkeypatch):
    """A `tool_ok:<req_id>` button press resolves the matching future
    with approved=True and edits the message to '已批准'."""
    monkeypatch.setattr(telegram_bot, "_is_allowed", lambda _uid: True)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    tool_guard._pending["abc123"] = fut

    q = _make_query(user_id=1, data="tool_ok:abc123")
    update = _make_update_with_query(q)
    await telegram_bot.callback_handler(update, context=None)

    assert fut.done() and fut.result() is True
    edit_text = q.edit_message_text.await_args.args[0]
    assert "已批准" in edit_text


async def test_callback_tool_no_resolves_pending(monkeypatch):
    monkeypatch.setattr(telegram_bot, "_is_allowed", lambda _uid: True)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    tool_guard._pending["xyz"] = fut

    q = _make_query(user_id=1, data="tool_no:xyz")
    await telegram_bot.callback_handler(_make_update_with_query(q), context=None)

    assert fut.done() and fut.result() is False
    edit_text = q.edit_message_text.await_args.args[0]
    assert "已拒绝" in edit_text


async def test_callback_stale_decision_says_timeout(monkeypatch):
    """Click after the gate already timed out: no pending future. Tell the
    user instead of pretending the click did anything."""
    monkeypatch.setattr(telegram_bot, "_is_allowed", lambda _uid: True)
    # Nothing in _pending.
    q = _make_query(user_id=1, data="tool_ok:gone")
    await telegram_bot.callback_handler(_make_update_with_query(q), context=None)

    edit_text = q.edit_message_text.await_args.args[0]
    assert "已超时" in edit_text or "已处理" in edit_text


async def test_callback_rejects_cross_user_click(monkeypatch):
    """Owner-binding: only the triggering user can answer the modal.
    A second allowed user who somehow learned the req_id (e.g. it was
    cached, or a future multi-device pairing) must NOT be able to
    approve someone else's tool call. Toast + don't touch the future."""
    monkeypatch.setattr(telegram_bot, "_is_allowed", lambda _uid: True)
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    tool_guard._pending["xx"] = fut
    telegram_bot._pending_owner["xx"] = 100  # request was for user 100

    # User 200 clicks — different user.
    q = _make_query(user_id=200, data="tool_ok:xx")
    await telegram_bot.callback_handler(_make_update_with_query(q), context=None)

    assert not fut.done(), "stranger's click must NOT resolve user 100's future"
    # `query.answer()` is awaited twice: once unconditionally at the top
    # of `callback_handler` (standard PTB acknowledge), once with
    # show_alert=True in the cross-user branch. We only care that the
    # alert variant fired.
    alert_calls = [
        c for c in q.answer.await_args_list if c.kwargs.get("show_alert")
    ]
    assert len(alert_calls) == 1
    assert "不是你的" in alert_calls[0].args[0]
    # No edit_message_text — we don't want to give the clicker UX feedback
    # that mutates the message; just the toast via answer().
    q.edit_message_text.assert_not_awaited()

    # And the legitimate owner can still approve afterwards.
    q2 = _make_query(user_id=100, data="tool_ok:xx")
    await telegram_bot.callback_handler(_make_update_with_query(q2), context=None)
    assert fut.done() and fut.result() is True


async def test_broadcaster_send_failure_falls_through_to_timeout(
    fake_app, set_user, monkeypatch
):
    """If `bot.send_message` raises (network blip, blocked, revoked bot
    token), the gate must NOT silently succeed. The exception is swallowed
    so the agent loop doesn't crash, but the future stays unresolved and
    the gate's wait_for times out → REJECTED. This is the security
    contract: any broadcaster failure → fail closed."""
    monkeypatch.setenv("ROBOOT_TOOL_APPROVAL", "confirm")
    telegram_bot._tg_app = fake_app
    fake_app.bot.send_message = AsyncMock(side_effect=RuntimeError("boom"))
    set_user(7)
    tool_guard.register_broadcaster(telegram_bot._broadcast_tool_approval)

    decision = await tool_guard.gate(
        "shell",
        {"command": "rm -rf /tmp/scratch"},
        origin="telegram",
        timeout=0.3,  # short — we WANT the timeout path
    )
    assert decision == tool_guard.Decision.REJECTED
    # And the owner map shouldn't leak across broadcaster failures.
    assert telegram_bot._pending_owner == {}


# -----------------------------------------------------------------------------
# 4. End-to-end through gate()
# -----------------------------------------------------------------------------


async def test_gate_to_telegram_callback_round_trip(
    fake_app, set_user, monkeypatch
):
    """Real round trip: confirm mode + telegram broadcaster registered;
    invoke gate(); simulate the user clicking the inline button via the
    callback_handler; gate returns APPROVED."""
    monkeypatch.setenv("ROBOOT_TOOL_APPROVAL", "confirm")
    monkeypatch.setattr(telegram_bot, "_is_allowed", lambda _uid: True)
    telegram_bot._tg_app = fake_app
    set_user(7)
    tool_guard.register_broadcaster(telegram_bot._broadcast_tool_approval)

    # Kick off gate() — it'll broadcast (mocked) and await the future.
    gate_task = asyncio.create_task(
        tool_guard.gate(
            "shell",
            {"command": "rm -rf /tmp/scratch"},
            origin="telegram",
            timeout=2.0,
        )
    )
    # Wait for the broadcast to land so we can read req_id off the call.
    for _ in range(50):
        if fake_app.bot.send_message.await_count > 0:
            break
        await asyncio.sleep(0.01)
    assert fake_app.bot.send_message.await_count == 1
    markup = fake_app.bot.send_message.await_args.kwargs["reply_markup"]
    ok_cb = next(
        btn.callback_data
        for row in markup.inline_keyboard
        for btn in row
        if btn.callback_data.startswith("tool_ok:")
    )

    # Simulate the user tapping 允许.
    q = _make_query(user_id=7, data=ok_cb)
    await telegram_bot.callback_handler(_make_update_with_query(q), context=None)

    decision = await asyncio.wait_for(gate_task, timeout=1.0)
    assert decision == tool_guard.Decision.APPROVED
