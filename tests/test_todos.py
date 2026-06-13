"""Tests for tools/todos.py — persistent todo list + scheduler hand-off."""

from __future__ import annotations

import time

import pytest

import tool_guard
from tools import scheduler, todos


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setattr(todos, "DB_PATH", tmp_path / "todos.db")
    monkeypatch.setattr(scheduler, "DB_PATH", tmp_path / "reminders.db")
    yield


@pytest.fixture
def as_origin():
    """Set tool_guard.current_origin to a given surface. Restores the default
    with a plain set() at teardown — reset(token) can't be used here because
    the to_thread hops inside the tools run in a different contextvars.Context
    than the fixture teardown."""

    def _set(o):
        tool_guard.current_origin.set(o)

    yield _set
    tool_guard.current_origin.set("local")  # default; next test starts clean


# ---------------------------------------------------------------------------
# add / list
# ---------------------------------------------------------------------------


async def test_add_and_list():
    out = await todos.add_todo("买牛奶")
    assert "已记下待办 #" in out and "买牛奶" in out
    listing = await todos.list_todos()
    assert "买牛奶" in listing and "未完成待办" in listing


async def test_add_validates():
    assert "不能为空" in await todos.add_todo("  ")
    assert "不能为负" in await todos.add_todo("x", -5)
    assert "太远" in await todos.add_todo("x", scheduler.MAX_DELAY_SECONDS + 1)


async def test_list_empty():
    assert "没有未完成" in await todos.list_todos()


# ---------------------------------------------------------------------------
# due_seconds hands a reminder to the scheduler
# ---------------------------------------------------------------------------


async def test_due_todo_creates_linked_reminder():
    out = await todos.add_todo("交报告", due_seconds=600)
    assert "截止" in out
    # A reminder row exists in the scheduler store for this todo.
    pending = scheduler._list_pending_sync(["local"])
    assert len(pending) == 1
    assert "待办到期: 交报告" == pending[0]["text"]


async def test_complete_cancels_linked_reminder():
    out = await todos.add_todo("交报告", due_seconds=600)
    tid = int(out.split("#")[1].split("：")[0])
    assert len(scheduler._list_pending_sync(["local"])) == 1

    done = await todos.complete_todo(tid)
    assert "已完成" in done
    # Linked reminder is gone, and the todo drops off the open list.
    assert scheduler._list_pending_sync(["local"]) == []
    assert "没有未完成" in await todos.list_todos()


async def test_cancel_deletes_todo_and_reminder():
    out = await todos.add_todo("可有可无", due_seconds=600)
    tid = int(out.split("#")[1].split("：")[0])
    msg = await todos.cancel_todo(tid)
    assert "已删除" in msg
    assert scheduler._list_pending_sync(["local"]) == []
    assert "没有未完成" in await todos.list_todos()


# ---------------------------------------------------------------------------
# origin isolation
# ---------------------------------------------------------------------------


async def test_origin_guard_blocks_cross_surface_complete(as_origin):
    as_origin("telegram")
    out = await todos.add_todo("telegram 的待办")
    tid = int(out.split("#")[1].split("：")[0])
    # A console (local) agent must not be able to complete a telegram todo.
    as_origin("local")
    assert "未找到" in await todos.complete_todo(tid)
    # And the telegram surface only sees its own.
    assert "没有未完成" in await todos.list_todos()  # local sees none
    as_origin("telegram")
    assert "telegram 的待办" in await todos.list_todos()


async def test_complete_unknown_id():
    assert "未找到" in await todos.complete_todo(99999)


async def test_telegram_users_isolated(as_origin):
    """Two Telegram users must not see/complete each other's todos."""
    from tools.voice_switch import current_tg_user

    as_origin("telegram")
    ta = current_tg_user.set(111)
    await todos.add_todo("A 的待办")
    current_tg_user.reset(ta)

    tb = current_tg_user.set(222)
    try:
        # B's list doesn't show A's.
        assert "没有未完成" in await todos.list_todos()
        await todos.add_todo("B 的待办")
        # B sees only B's.
        lst = await todos.list_todos()
        assert "B 的待办" in lst and "A 的待办" not in lst
        # B can't complete A's todo (#1).
        assert "未找到" in await todos.complete_todo(1)
    finally:
        current_tg_user.reset(tb)


async def test_add_todo_coerces_string_due():
    out = await todos.add_todo("交报告", due_seconds="600")
    assert "已记下待办 #" in out and "截止" in out
    assert len(scheduler._list_pending_sync(["local"])) == 1


async def test_add_todo_rejects_garbage_due():
    assert "整数" in await todos.add_todo("x", due_seconds="abc")
    assert "整数" in await todos.complete_todo("notanid")


# ---------------------------------------------------------------------------
# security: .todos.db is on the files.py deny-list
# ---------------------------------------------------------------------------


def test_todos_db_is_secret_in_files():
    from tools import files

    assert files._deny_reason(".todos.db", for_write=False) is not None
    assert files._deny_reason(".todos.db", for_write=True) is not None
