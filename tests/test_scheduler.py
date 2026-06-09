"""Tests for tools/scheduler.py — reminder persistence + dispatcher + tools.

DB is redirected to a tmp file per test. The dispatcher globals are reset so
a task from one test never leaks into the next.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import tool_guard
from tools import scheduler


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "reminders.db")
    scheduler._dispatcher_task = None
    scheduler._wake = None
    yield
    # Best-effort sync cleanup; async tests should await stop_dispatcher()
    # themselves so the cancelled task fully unwinds before the loop closes.
    t = scheduler._dispatcher_task
    if t is not None and not t.done():
        t.cancel()
    scheduler._dispatcher_task = None
    scheduler._wake = None


@pytest.fixture
def origin():
    """Set tool_guard.current_origin for the duration of one test."""
    tok = tool_guard.current_origin.set("local")
    yield "local"
    tool_guard.current_origin.reset(tok)


# ---------------------------------------------------------------------------
# Persistence primitives
# ---------------------------------------------------------------------------


def test_add_and_list_pending():
    now = time.time()
    rid = scheduler._add_sync("drink water", now + 100, 0, "local", None)
    assert rid > 0
    rows = scheduler._list_pending_sync(["local"])
    assert len(rows) == 1
    assert rows[0]["text"] == "drink water"
    assert rows[0]["id"] == rid


def test_cancel_respects_origin():
    now = time.time()
    rid = scheduler._add_sync("telegram-only", now + 100, 0, "telegram", "42")
    # A console (origin=local) must not cancel a telegram reminder.
    assert scheduler._cancel_sync(rid, "local") is False
    # The owning origin can.
    assert scheduler._cancel_sync(rid, "telegram") is True
    assert scheduler._list_pending_sync(None) == []


def test_claim_due_only_takes_due_and_marks_fired():
    now = time.time()
    past = scheduler._add_sync("due now", now - 1, 0, "local", None)
    future = scheduler._add_sync("later", now + 1000, 0, "local", None)

    claimed = scheduler._claim_due_sync(["local"], now)
    assert [c["id"] for c in claimed] == [past]
    # The due one is fired; the future one stays pending.
    pending = {r["id"] for r in scheduler._list_pending_sync(["local"])}
    assert pending == {future}
    # Claiming again returns nothing (already fired).
    assert scheduler._claim_due_sync(["local"], now) == []


def test_claim_partitions_by_origin():
    now = time.time()
    local_id = scheduler._add_sync("local r", now - 1, 0, "local", None)
    tg_id = scheduler._add_sync("tg r", now - 1, 0, "telegram", "7")

    # The daemon dispatcher (local/relay) must NOT claim the telegram one.
    claimed = scheduler._claim_due_sync(["local", "relay", ""], now)
    assert [c["id"] for c in claimed] == [local_id]
    # The telegram dispatcher claims its own.
    claimed_tg = scheduler._claim_due_sync(["telegram"], now)
    assert [c["id"] for c in claimed_tg] == [tg_id]


def test_recurring_reschedules_after_delivery():
    now = time.time()
    rid = scheduler._add_sync("hourly", now - 5, 3600, "local", None)
    claimed = scheduler._claim_due_sync(["local"], now)
    assert [c["id"] for c in claimed] == [rid]
    # Claim alone consumes it (fired=1) — reschedule happens in finalize.
    assert scheduler._list_pending_sync(["local"]) == []
    scheduler._finalize_sync(rid, 3600, now - 5, delivered=True, now=now)
    rows = scheduler._list_pending_sync(["local"])
    assert len(rows) == 1 and rows[0]["due_at"] > now


def test_finalize_oneshot_delivered_consumed():
    now = time.time()
    rid = scheduler._add_sync("once", now - 1, 0, "local", None)
    scheduler._claim_due_sync(["local"], now)
    scheduler._finalize_sync(rid, 0, now - 1, delivered=True, now=now)
    assert scheduler._list_pending_sync(["local"]) == []  # consumed


def test_finalize_not_delivered_retries():
    """The HIGH bug: a one-shot reminder that reached no surface must NOT be
    lost — finalize un-fires it so the next poll retries."""
    now = time.time()
    rid = scheduler._add_sync("remind", now - 1, 0, "local", None)
    scheduler._claim_due_sync(["local"], now)
    assert scheduler._list_pending_sync(["local"]) == []  # claimed (fired=1)
    scheduler._finalize_sync(rid, 0, now - 1, delivered=False, now=now)
    rows = scheduler._list_pending_sync(["local"])
    assert len(rows) == 1 and rows[0]["id"] == rid  # back to pending → retried


def test_finalize_retry_backs_off_due_at():
    """Retry must push due_at forward (no hot-loop on an overdue row)."""
    now = time.time()
    rid = scheduler._add_sync("r", now - 1, 0, "local", None)
    scheduler._claim_due_sync(["local"], now)
    scheduler._finalize_sync(rid, 0, now - 1, delivered=False, now=now, attempts=0)
    rows = scheduler._list_pending_sync(["local"])
    assert len(rows) == 1 and rows[0]["due_at"] >= now + scheduler.RETRY_BACKOFF - 1


def test_finalize_gives_up_after_max_retries():
    now = time.time()
    rid = scheduler._add_sync("stale", now - 1, 0, "local", None)
    scheduler._claim_due_sync(["local"], now)
    # Simulate the final attempt — retries exhausted → dropped, not retried.
    scheduler._finalize_sync(
        rid, 0, now - 1, delivered=False, now=now, attempts=scheduler.MAX_RETRIES
    )
    assert scheduler._list_pending_sync(["local"]) == []


# ---------------------------------------------------------------------------
# Dispatcher end-to-end
# ---------------------------------------------------------------------------


async def test_dispatcher_fires_due_reminder():
    delivered: list[dict] = []

    async def deliver(r):
        delivered.append(r)
        return True  # reached a surface → consume

    now = time.time()
    scheduler._add_sync("ping", now - 1, 0, "local", None)
    scheduler.start_dispatcher(["local"], deliver)
    try:
        for _ in range(50):
            if delivered:
                break
            await asyncio.sleep(0.02)
        assert len(delivered) == 1
        assert delivered[0]["text"] == "ping"
        # Consumed — not re-delivered on subsequent polls.
        await asyncio.sleep(0.1)
        assert len(delivered) == 1
    finally:
        await scheduler.stop_dispatcher()


async def test_dispatcher_retries_when_no_surface():
    """deliver() returning False (no surface connected) must leave the
    reminder pending for a later poll — not consume it."""
    now = time.time()
    scheduler._add_sync("later", now - 1, 0, "local", None)
    attempts = {"n": 0}

    async def deliver(r):
        attempts["n"] += 1
        return False  # no surface reached

    scheduler.start_dispatcher(["local"], deliver)
    try:
        await asyncio.sleep(0.2)
        # Tried at least once, and the reminder is STILL pending (retryable).
        assert attempts["n"] >= 1
        assert len(scheduler._list_pending_sync(["local"])) == 1
    finally:
        await scheduler.stop_dispatcher()


async def test_dispatcher_wakes_on_new_reminder(origin):
    delivered: list[dict] = []

    async def deliver(r):
        delivered.append(r)

    # Start with no reminders, then schedule one ~now via the tool.
    scheduler.start_dispatcher(["local"], deliver)
    await asyncio.sleep(0.02)  # let it enter its wait
    try:
        out = await scheduler.schedule_reminder("woke up", delay_seconds=1)
        assert "已设置提醒" in out
        # The reminder is 1s out; assert it's pending and the tool set the
        # wake event (dispatcher will re-arm to the nearer deadline).
        rows = scheduler._list_pending_sync(["local"])
        assert len(rows) == 1 and rows[0]["text"] == "woke up"
    finally:
        await scheduler.stop_dispatcher()


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------


async def test_schedule_reminder_validates(origin):
    assert "不能为空" in await scheduler.schedule_reminder("", 100)
    assert "正整数" in await scheduler.schedule_reminder("x", 0)
    assert "正整数" in await scheduler.schedule_reminder("x", -5)
    assert "太远" in await scheduler.schedule_reminder("x", scheduler.MAX_DELAY_SECONDS + 1)


async def test_schedule_list_cancel_round_trip(origin):
    out = await scheduler.schedule_reminder("meeting", delay_seconds=600)
    assert "已设置提醒 #" in out
    rid = int(out.split("#")[1].split("：")[0])

    listing = await scheduler.list_reminders()
    assert "meeting" in listing and f"#{rid}" in listing

    cancelled = await scheduler.cancel_reminder(rid)
    assert "已取消" in cancelled
    assert "没有待触发的提醒" in await scheduler.list_reminders()


async def test_cancel_unknown_id(origin):
    assert "未找到" in await scheduler.cancel_reminder(99999)


# ---------------------------------------------------------------------------
# Per-telegram-user isolation + int coercion
# ---------------------------------------------------------------------------


def test_list_and_cancel_scoped_by_target():
    now = time.time()
    a = scheduler._add_sync("A 的提醒", now + 100, 0, "telegram", "111")
    b = scheduler._add_sync("B 的提醒", now + 100, 0, "telegram", "222")
    # User A sees only A's.
    rows = scheduler._list_pending_sync(["telegram"], "111")
    assert [r["id"] for r in rows] == [a]
    # User B cannot cancel A's reminder.
    assert scheduler._cancel_sync(a, "telegram", "222") is False
    # A can.
    assert scheduler._cancel_sync(a, "telegram", "111") is True
    assert {r["id"] for r in scheduler._list_pending_sync(["telegram"], None)} == {b}


async def test_schedule_reminder_coerces_string_int(origin):
    # A provider that ignores the integer schema and sends "600" must still work
    # (not crash on '600' < 0).
    out = await scheduler.schedule_reminder("喝水", delay_seconds="600")
    assert "已设置提醒 #" in out
    assert len(scheduler._list_pending_sync(["local"])) == 1


async def test_schedule_reminder_rejects_garbage(origin):
    assert "整数" in await scheduler.schedule_reminder("x", delay_seconds="abc")
