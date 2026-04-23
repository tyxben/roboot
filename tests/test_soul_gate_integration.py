"""Integration tests for the review gate as wired into `tools/soul.py`.

These go one level up from `test_soul_review.py`: we exercise the actual
Arcana-facing tools (`update_self`, `remember_user`, `add_note`, the
`remember_user_automated` helper, and the sync `append_self_feedback`
distiller path), and assert the on-disk `soul.md` state.

All filesystem roots (`SOUL_PATH`, `SOUL_HISTORY_DIR`, `PENDING_DIR`) are
redirected into `tmp_path`, and the module-level broadcaster list is
cleaned between tests so nothing leaks.

The WebSocket wiring and frontend modal are NOT under test here — those
surfaces are covered elsewhere. This file's scope is purely
"does the gate decide correctly, and does the tool honor that decision?"
"""

from __future__ import annotations

import asyncio

import pytest

import soul_review
from tools import soul as soul_mod


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


_SEED_SOUL = (
    "# Ava\n\n"
    "## Identity\n\n"
    "- **Name**: Ava\n"
    "- **Voice**: zh-CN-YunxiNeural\n\n"
    "## Personality\n\n"
    "- curious\n\n"
    "## Speaking Style\n\n"
    "- 简短\n\n"
    "## About User\n\n"
    "（通过对话自然积累）\n\n"
    "## Notes\n\n"
    "（自己的想法和记录）\n"
)


@pytest.fixture
def tmp_soul(tmp_path, monkeypatch):
    """Redirect soul.md + history + pending dirs into tmp_path, seed a
    minimal soul.md, and clear module-level gate state."""
    soul_path = tmp_path / "soul.md"
    history_dir = tmp_path / ".soul" / "history"
    pending_dir = tmp_path / ".soul" / "pending"

    monkeypatch.setattr(soul_mod, "SOUL_PATH", soul_path)
    monkeypatch.setattr(soul_mod, "SOUL_HISTORY_DIR", history_dir)
    monkeypatch.setattr(soul_review, "PENDING_DIR", pending_dir)

    soul_path.write_text(_SEED_SOUL)

    # Make sure no broadcasters or pending futures leak in.
    soul_review._broadcasters.clear()
    soul_review._pending.clear()

    yield soul_path

    soul_review._broadcasters.clear()
    for fut in list(soul_review._pending.values()):
        if not fut.done():
            fut.cancel()
    soul_review._pending.clear()


def _install_auto_responder(approve: bool):
    """Register a broadcaster that immediately resolves the review with the
    given verdict. Returns the list where captured frames are recorded."""
    captured = []

    async def bc(frame):
        captured.append(frame)
        # Schedule the resolution on the running loop so review_write's
        # awaited future actually gets woken up.
        loop = asyncio.get_running_loop()
        loop.call_soon(
            soul_review.resolve_decision, frame["req_id"], approve
        )

    soul_review.register_broadcaster(bc)
    return captured


# -----------------------------------------------------------------------------
# Mode OFF: writes pass straight through.
# -----------------------------------------------------------------------------


async def test_update_self_off_mode_writes(tmp_soul, monkeypatch):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "off")

    result = await soul_mod.update_self("name", "Foo")
    assert "Foo" in result
    content = tmp_soul.read_text()
    assert "**Name**: Foo" in content


# -----------------------------------------------------------------------------
# Mode CONFIRM: approval + rejection paths
# -----------------------------------------------------------------------------


async def test_update_self_confirm_approved_writes(tmp_soul, monkeypatch):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")
    captured = _install_auto_responder(approve=True)

    result = await soul_mod.update_self("name", "Approved")
    # User-facing confirmation (not a rejection message).
    assert "Approved" in result
    assert "拒绝" not in result
    # File was written.
    assert "**Name**: Approved" in tmp_soul.read_text()
    # Broadcaster saw exactly one frame, origin=update_self.
    assert len(captured) == 1
    assert captured[0]["origin"] == "update_self"


async def test_update_self_confirm_rejected_does_not_write(
    tmp_soul, monkeypatch
):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")
    _install_auto_responder(approve=False)

    original = tmp_soul.read_text()
    result = await soul_mod.update_self("name", "Rejected")
    # File untouched.
    assert tmp_soul.read_text() == original
    # Caller sees a rejection string.
    assert "被拒绝" in result


# -----------------------------------------------------------------------------
# Sync path (append_self_feedback) — CONFIRM degrades to LOG, no broadcast.
# -----------------------------------------------------------------------------


def test_append_self_feedback_confirm_degrades_to_log(tmp_soul, monkeypatch):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    bc_calls = []

    async def bc(frame):
        bc_calls.append(frame)

    soul_review.register_broadcaster(bc)

    soul_mod.append_self_feedback("试了 X，确实更顺")

    # Written despite CONFIRM, because the sync path degrades.
    assert "试了 X，确实更顺" in tmp_soul.read_text()
    # No broadcast fired (sync context cannot await one).
    assert bc_calls == []


# -----------------------------------------------------------------------------
# automated=True: CONFIRM degrades to LOG, no broadcast.
# -----------------------------------------------------------------------------


async def test_remember_user_automated_bypasses_confirm_modal(
    tmp_soul, monkeypatch
):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    bc_calls = []

    async def bc(frame):
        bc_calls.append(frame)

    soul_review.register_broadcaster(bc)

    result = await soul_mod.remember_user_automated("用户喜欢简短")
    assert "用户喜欢简短" in result
    assert "用户喜欢简短" in tmp_soul.read_text()
    # Automated flag → no modal broadcast.
    assert bc_calls == []


# -----------------------------------------------------------------------------
# Interactive remember_user DOES fire a broadcast under CONFIRM.
# -----------------------------------------------------------------------------


async def test_remember_user_confirm_fires_broadcast(tmp_soul, monkeypatch):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")
    captured = _install_auto_responder(approve=True)

    # arcana.tool wraps the function; call the underlying coroutine.
    fn = getattr(soul_mod.remember_user, "fn", soul_mod.remember_user)
    result = await fn("用户在北京")

    assert "用户在北京" in result
    assert "用户在北京" in tmp_soul.read_text()
    # Exactly one frame, origin=remember_user.
    assert len(captured) == 1
    assert captured[0]["origin"] == "remember_user"
