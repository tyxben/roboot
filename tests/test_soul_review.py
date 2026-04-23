"""Tests for the soul.md review gate (`soul_review.py`).

The gate has three modes (OFF/LOG/CONFIRM) and emits a Decision
(AUTO/LOGGED/APPROVED/REJECTED). CONFIRM mode broadcasts a `soul_review`
frame to registered broadcasters and awaits a `resolve_decision` reply,
with a timeout and a 2 KB diff ceiling.

Every test redirects `PENDING_DIR` under `tmp_path` so no real files are
written, and clears module-level broadcaster / pending-future state
between tests so there's no cross-test contamination.

Only `soul_review` is under test here — the WS wiring and frontend modal
are covered elsewhere.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import soul_review
from soul_review import Decision, Mode


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_pending_dir(tmp_path, monkeypatch):
    """Redirect PENDING_DIR into a per-test tmp path so the real
    `.soul/pending/` is never touched."""
    pending = tmp_path / "pending"
    monkeypatch.setattr(soul_review, "PENDING_DIR", pending)
    return pending


@pytest.fixture(autouse=True)
def _reset_module_state():
    """Reset the module-level broadcaster list + pending futures so state
    from a previous test can't leak into the next one."""
    # Before: make sure we start clean.
    soul_review._broadcasters.clear()
    soul_review._pending.clear()
    yield
    # After: clear again in case the test registered something and raised.
    soul_review._broadcasters.clear()
    # Cancel any outstanding futures so we don't leak warnings.
    for fut in list(soul_review._pending.values()):
        if not fut.done():
            fut.cancel()
    soul_review._pending.clear()


# -----------------------------------------------------------------------------
# get_mode
# -----------------------------------------------------------------------------


def test_get_mode_default_off(monkeypatch):
    monkeypatch.delenv("ROBOOT_SOUL_REVIEW", raising=False)
    assert soul_review.get_mode() == Mode.OFF


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("log", Mode.LOG),
        ("confirm", Mode.CONFIRM),
        ("OFF", Mode.OFF),
        (" log ", Mode.LOG),
        ("CONFIRM", Mode.CONFIRM),
    ],
)
def test_get_mode_case_and_whitespace(monkeypatch, raw, expected):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", raw)
    assert soul_review.get_mode() == expected


def test_get_mode_unknown_falls_back_to_off(monkeypatch, caplog):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "nonsense")
    with caplog.at_level(logging.WARNING, logger=soul_review.logger.name):
        mode = soul_review.get_mode()
    assert mode == Mode.OFF
    assert any("nonsense" in rec.message or "nonsense" in str(rec.args)
               for rec in caplog.records)


# -----------------------------------------------------------------------------
# review_write — mode OFF
# -----------------------------------------------------------------------------


async def test_review_write_off_returns_auto(monkeypatch, _isolate_pending_dir):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "off")

    bc_calls = []

    async def bc(frame):
        bc_calls.append(frame)

    soul_review.register_broadcaster(bc)

    decision = await soul_review.review_write(
        "before text\n", "after text\n", origin="update_self"
    )
    assert decision == Decision.AUTO
    assert bc_calls == []
    assert not _isolate_pending_dir.exists() or not any(_isolate_pending_dir.iterdir())


# -----------------------------------------------------------------------------
# review_write — mode LOG
# -----------------------------------------------------------------------------


async def test_review_write_log_writes_diff_file(monkeypatch, _isolate_pending_dir):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "log")

    bc_calls = []

    async def bc(frame):
        bc_calls.append(frame)

    soul_review.register_broadcaster(bc)

    decision = await soul_review.review_write(
        "hello\n", "goodbye\n", origin="update_self"
    )
    assert decision == Decision.LOGGED
    # No broadcast in LOG mode.
    assert bc_calls == []
    # Exactly one diff file written.
    files = list(_isolate_pending_dir.iterdir())
    assert len(files) == 1
    content = files[0].read_text()
    # Unified diff contents: both sides + the diff lines.
    assert "hello" in content
    assert "goodbye" in content
    assert "update_self" in files[0].name


# -----------------------------------------------------------------------------
# review_write — mode CONFIRM, no broadcasters
# -----------------------------------------------------------------------------


async def test_review_write_confirm_no_broadcaster_degrades_to_log(
    monkeypatch, _isolate_pending_dir, caplog
):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    with caplog.at_level(logging.WARNING, logger=soul_review.logger.name):
        decision = await soul_review.review_write(
            "a\n", "b\n", origin="update_self"
        )
    assert decision == Decision.LOGGED
    files = list(_isolate_pending_dir.iterdir())
    assert len(files) == 1
    assert "no broadcasters" in " ".join(r.getMessage() for r in caplog.records).lower()


# -----------------------------------------------------------------------------
# review_write — mode CONFIRM, broadcaster + approval / rejection / timeout
# -----------------------------------------------------------------------------


async def test_review_write_confirm_approved(monkeypatch, _isolate_pending_dir):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    captured_frame = {}
    frame_arrived = asyncio.Event()

    async def bc(frame):
        captured_frame.update(frame)
        frame_arrived.set()

    soul_review.register_broadcaster(bc)

    task = asyncio.create_task(
        soul_review.review_write(
            "before\n", "after\n", origin="update_self", timeout=1.0
        )
    )

    # Wait until the broadcaster actually fired so we know req_id is populated.
    await asyncio.wait_for(frame_arrived.wait(), timeout=0.5)

    assert captured_frame["type"] == "soul_review"
    assert captured_frame["origin"] == "update_self"
    assert "before" in captured_frame["diff"]
    assert "after" in captured_frame["diff"]
    assert captured_frame["timeout_s"] == 1.0
    req_id = captured_frame["req_id"]
    assert isinstance(req_id, str) and len(req_id) > 0

    assert soul_review.resolve_decision(req_id, True) is True

    decision = await asyncio.wait_for(task, timeout=0.5)
    assert decision == Decision.APPROVED
    # Approval does NOT write a REJECTED diff — approved writes aren't logged.
    rejected_files = [
        p for p in _isolate_pending_dir.iterdir() if "REJECTED" in p.name
    ] if _isolate_pending_dir.exists() else []
    assert rejected_files == []


async def test_review_write_confirm_rejected(monkeypatch, _isolate_pending_dir):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    frame_arrived = asyncio.Event()
    captured = {}

    async def bc(frame):
        captured.update(frame)
        frame_arrived.set()

    soul_review.register_broadcaster(bc)

    task = asyncio.create_task(
        soul_review.review_write(
            "x\n", "y\n", origin="remember_user", timeout=1.0
        )
    )
    await asyncio.wait_for(frame_arrived.wait(), timeout=0.5)

    assert soul_review.resolve_decision(captured["req_id"], False) is True

    decision = await asyncio.wait_for(task, timeout=0.5)
    assert decision == Decision.REJECTED

    files = list(_isolate_pending_dir.iterdir())
    # Exactly one file, and the origin reflects the rejection suffix.
    assert len(files) == 1
    assert "remember_user-REJECTED" in files[0].name


async def test_review_write_confirm_timeout(monkeypatch, _isolate_pending_dir):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    async def bc(_frame):
        # Accept the frame, but never resolve.
        return

    soul_review.register_broadcaster(bc)

    decision = await soul_review.review_write(
        "foo\n", "bar\n", origin="add_note", timeout=0.05
    )
    assert decision == Decision.REJECTED

    files = list(_isolate_pending_dir.iterdir())
    assert len(files) == 1
    assert "TIMEOUT" in files[0].name


# -----------------------------------------------------------------------------
# Oversize diff
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["log", "confirm"])
async def test_review_write_oversize_rejected(
    monkeypatch, _isolate_pending_dir, mode
):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", mode)

    before = "a\n"
    # A single line > MAX_DIFF_BYTES ensures the diff itself overshoots.
    after = ("Z" * (soul_review.MAX_DIFF_BYTES + 500)) + "\n"

    bc_calls = []

    async def bc(frame):
        bc_calls.append(frame)

    soul_review.register_broadcaster(bc)

    decision = await soul_review.review_write(
        before, after, origin="update_self", timeout=0.1
    )
    assert decision == Decision.REJECTED
    # Broadcaster must never see oversized diffs.
    assert bc_calls == []
    files = list(_isolate_pending_dir.iterdir())
    assert len(files) == 1
    assert "REJECTED-OVERSIZE" in files[0].name


# -----------------------------------------------------------------------------
# automated=True degrades CONFIRM to LOG
# -----------------------------------------------------------------------------


async def test_review_write_automated_degrades_confirm_to_log(
    monkeypatch, _isolate_pending_dir
):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    bc_calls = []

    async def bc(frame):
        bc_calls.append(frame)

    soul_review.register_broadcaster(bc)

    decision = await soul_review.review_write(
        "a\n", "b\n", origin="distill", automated=True, timeout=0.1
    )
    assert decision == Decision.LOGGED
    assert bc_calls == []
    files = list(_isolate_pending_dir.iterdir())
    assert len(files) == 1
    assert "distill" in files[0].name


# -----------------------------------------------------------------------------
# Empty diff (before == after)
# -----------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["off", "log", "confirm"])
async def test_review_write_empty_diff_auto(
    monkeypatch, _isolate_pending_dir, mode
):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", mode)

    bc_calls = []

    async def bc(frame):
        bc_calls.append(frame)

    soul_review.register_broadcaster(bc)

    decision = await soul_review.review_write(
        "same\n", "same\n", origin="update_self", timeout=0.1
    )
    assert decision == Decision.AUTO
    assert bc_calls == []
    # No file should ever be written for a no-op change.
    if _isolate_pending_dir.exists():
        assert list(_isolate_pending_dir.iterdir()) == []


# -----------------------------------------------------------------------------
# review_write_sync
# -----------------------------------------------------------------------------


def test_review_write_sync_off(monkeypatch, _isolate_pending_dir):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "off")
    decision = soul_review.review_write_sync(
        "a\n", "b\n", origin="self_feedback"
    )
    assert decision == Decision.AUTO
    if _isolate_pending_dir.exists():
        assert list(_isolate_pending_dir.iterdir()) == []


def test_review_write_sync_log(monkeypatch, _isolate_pending_dir):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "log")
    decision = soul_review.review_write_sync(
        "a\n", "b\n", origin="self_feedback"
    )
    assert decision == Decision.LOGGED
    files = list(_isolate_pending_dir.iterdir())
    assert len(files) == 1
    assert "self_feedback" in files[0].name


def test_review_write_sync_confirm_degrades_to_log(
    monkeypatch, _isolate_pending_dir
):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    bc_calls = []

    async def bc(frame):
        bc_calls.append(frame)

    # Even with a broadcaster registered, sync path must not call it.
    soul_review.register_broadcaster(bc)

    decision = soul_review.review_write_sync(
        "a\n", "b\n", origin="self_feedback"
    )
    assert decision == Decision.LOGGED
    assert bc_calls == []
    files = list(_isolate_pending_dir.iterdir())
    assert len(files) == 1


def test_review_write_sync_empty_diff(monkeypatch, _isolate_pending_dir):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "log")
    decision = soul_review.review_write_sync(
        "same\n", "same\n", origin="self_feedback"
    )
    assert decision == Decision.AUTO
    if _isolate_pending_dir.exists():
        assert list(_isolate_pending_dir.iterdir()) == []


# -----------------------------------------------------------------------------
# resolve_decision semantics
# -----------------------------------------------------------------------------


def test_resolve_decision_unknown_req_id_returns_false():
    assert soul_review.resolve_decision("not-a-real-id", True) is False


async def test_resolve_decision_twice_returns_false_second_time(monkeypatch):
    monkeypatch.setenv("ROBOOT_SOUL_REVIEW", "confirm")

    frame_arrived = asyncio.Event()
    captured = {}

    async def bc(frame):
        captured.update(frame)
        frame_arrived.set()

    soul_review.register_broadcaster(bc)

    task = asyncio.create_task(
        soul_review.review_write("a\n", "b\n", origin="x", timeout=1.0)
    )
    await asyncio.wait_for(frame_arrived.wait(), timeout=0.5)

    req_id = captured["req_id"]
    assert soul_review.resolve_decision(req_id, True) is True
    # The future is popped on first resolve; a stale click comes back False.
    assert soul_review.resolve_decision(req_id, True) is False

    await asyncio.wait_for(task, timeout=0.5)


# -----------------------------------------------------------------------------
# register / unregister broadcaster
# -----------------------------------------------------------------------------


def test_register_broadcaster_is_idempotent():
    async def bc(_f):
        return None

    soul_review.register_broadcaster(bc)
    soul_review.register_broadcaster(bc)
    assert soul_review._broadcasters.count(bc) == 1


def test_unregister_broadcaster_not_present_is_silent():
    async def bc(_f):
        return None

    # Not registered; should not raise.
    soul_review.unregister_broadcaster(bc)
    assert bc not in soul_review._broadcasters


def test_register_then_unregister_removes_entry():
    async def bc(_f):
        return None

    soul_review.register_broadcaster(bc)
    assert bc in soul_review._broadcasters
    soul_review.unregister_broadcaster(bc)
    assert bc not in soul_review._broadcasters
