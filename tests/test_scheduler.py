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


def test_recurring_reschedules_to_future_slot():
    now = time.time()
    rid = scheduler._add_sync("hourly", now - 5, 3600, "local", None)
    claimed = scheduler._claim_due_sync(["local"], now)
    assert [c["id"] for c in claimed] == [rid]
    # Still pending (recurring), with due_at advanced into the future.
    rows = scheduler._list_pending_sync(["local"])
    assert len(rows) == 1
    assert rows[0]["due_at"] > now


# ---------------------------------------------------------------------------
# Dispatcher end-to-end
# ---------------------------------------------------------------------------


async def test_dispatcher_fires_due_reminder():
    delivered: list[dict] = []

    async def deliver(r):
        delivered.append(r)

    now = time.time()
    scheduler._add_sync("ping", now - 1, 0, "local", None)
    scheduler.start_dispatcher(["local"], deliver)
    try:
        # Give the loop a few ticks to claim + deliver.
        for _ in range(50):
            if delivered:
                break
            await asyncio.sleep(0.02)
        assert len(delivered) == 1
        assert delivered[0]["text"] == "ping"
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
