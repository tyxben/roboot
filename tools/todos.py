"""Persistent, checkable todo list.

soul.md holds soft knowledge; todos are discrete checkable items, so they live
in their own SQLite store (.todos.db) — same autocommit+WAL+busy_timeout+
closing() shape as tools/scheduler.py. A todo with a due time hands a reminder
off to the scheduler's existing dispatcher (no new delivery path), so "明天提醒
我交报告" fires on the surface it was created from, local==remote for free.

Security: writes are local sqlite, side_effect="write" but NOT in tool_guard's
always-confirm set (benign internal write, same as schedule_reminder). .todos.db
is added to tools/files.py's secret deny-list so read_file/write_file can't be
used to bypass the per-origin isolation enforced here. complete/cancel carry an
origin guard so a Telegram user can't check off a console todo and vice-versa.
"""

import asyncio
import logging
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import arcana

from tools import scheduler

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / ".todos.db"

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS todos (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  text        TEXT NOT NULL,
  done        INTEGER NOT NULL DEFAULT 0,
  due_at      REAL,
  origin      TEXT NOT NULL DEFAULT '',
  target      TEXT,
  reminder_id INTEGER,
  created_at  REAL NOT NULL,
  done_at     REAL
);
CREATE INDEX IF NOT EXISTS idx_todos_open ON todos(done, origin);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")  # 3rd multi-process writer
    conn.executescript(_INIT_SQL)
    return conn


# ---------------------------------------------------------------------------
# Sync DB ops
# ---------------------------------------------------------------------------


def _add_sync(
    text: str, due_at: float | None, origin: str, target: str | None, reminder_id: int | None
) -> int:
    with closing(_connect()) as conn:
        cur = conn.execute(
            "INSERT INTO todos(text, done, due_at, origin, target, reminder_id,"
            " created_at) VALUES (?, 0, ?, ?, ?, ?, ?)",
            (text, due_at, origin, target, reminder_id, time.time()),
        )
        return int(cur.lastrowid)


def _scope(origin: str | None, target: str | None) -> tuple[str, list]:
    """SQL fragment + args scoping a todo to (origin, target). target adds the
    per-user scope within a surface (Telegram) so one user can't touch another
    user's todos even though they share origin='telegram'."""
    parts: list[str] = []
    args: list = []
    if origin is not None:
        parts.append("origin=?")
        args.append(origin)
    if target is not None:
        parts.append("target=?")
        args.append(target)
    return ("".join(" AND " + p for p in parts), args)


def _link_reminder_sync(todo_id: int, reminder_id: int) -> None:
    with closing(_connect()) as conn:
        conn.execute(
            "UPDATE todos SET reminder_id=? WHERE id=?", (reminder_id, todo_id)
        )


def _list_open_sync(origin: str | None, target: str | None = None) -> list[dict]:
    scope, args = _scope(origin, target)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT id, text, due_at FROM todos WHERE done=0" + scope
            + " ORDER BY (due_at IS NULL), due_at ASC, id ASC",
            tuple(args),
        ).fetchall()
    return [{"id": r[0], "text": r[1], "due_at": r[2]} for r in rows]


def _close_sync(
    todo_id: int, origin: str | None, target: str | None = None, *, delete: bool
) -> tuple[bool, int | None]:
    """Complete (done=1) or delete an OPEN todo, scoped to (origin, target).
    Returns (ok, linked_reminder_id) so the caller can cancel a pending
    reminder."""
    scope, args = _scope(origin, target)
    with closing(_connect()) as conn:
        row = conn.execute(
            "SELECT reminder_id FROM todos WHERE id=? AND done=0" + scope,
            (todo_id, *args),
        ).fetchone()
        if row is None:
            return False, None
        reminder_id = row[0]
        if delete:
            conn.execute("DELETE FROM todos WHERE id=?", (todo_id,))
        else:
            conn.execute(
                "UPDATE todos SET done=1, done_at=? WHERE id=?", (time.time(), todo_id)
            )
        return True, reminder_id


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------


def _fmt_due(due_at: float | None) -> str:
    if not due_at:
        return ""
    return "（截止 " + time.strftime("%m-%d %H:%M", time.localtime(due_at)) + "）"


@arcana.tool(
    when_to_use=(
        "当用户要你记一件待办的事时，例如'记一下我要交报告'、'提醒我买牛奶'、"
        "'帮我列个 todo'。可选给 due_seconds（从现在起多少秒后到期），到期会自动提醒。"
    ),
    what_to_expect="确认信息，含待办编号和截止时间",
    failure_meaning="内容为空或写入失败",
    side_effect="write",
)
async def add_todo(text: str, due_seconds: int = 0) -> str:
    """添加一条待办。due_seconds>0 时到期会通过提醒系统通知。"""
    text = (text or "").strip()
    if not text:
        return "待办内容不能为空"
    due_seconds = scheduler.coerce_int(due_seconds, default=0)
    if due_seconds is None:
        return "due_seconds 必须是整数（从现在起多少秒）"
    if due_seconds < 0:
        return "due_seconds 不能为负"
    if due_seconds > scheduler.MAX_DELAY_SECONDS:
        return "截止时间太远了（超过一年）"
    origin = scheduler._current_origin()
    target = scheduler._current_target(origin)
    due_at = time.time() + float(due_seconds) if due_seconds else None
    # Insert the todo FIRST (reminder_id NULL), then create the linked reminder,
    # then back-link it. If reminder creation fails, the todo still exists with
    # no due notification — never a reminder orphaned from a non-existent todo.
    tid = await asyncio.to_thread(_add_sync, text, due_at, origin, target, None)
    if due_at is not None:
        try:
            reminder_id = await asyncio.to_thread(
                scheduler._add_sync, f"待办到期: {text}", due_at, 0.0, origin, target
            )
            await asyncio.to_thread(_link_reminder_sync, tid, reminder_id)
            scheduler._get_wake().set()
        except Exception as e:
            logger.warning("todo #%s: due reminder not set: %s", tid, e)
            return f"已记下待办 #{tid}：{text}（提醒未能设置）"
    return f"已记下待办 #{tid}：{text}{_fmt_due(due_at)}"


@arcana.tool(
    when_to_use="当用户想看自己有哪些待办没完成时",
    what_to_expect="未完成的待办列表（编号、内容、截止时间）",
    failure_meaning="读取失败",
    side_effect="read",
)
async def list_todos() -> str:
    """列出当前所有未完成的待办。"""
    origin = scheduler._current_origin()
    target = scheduler._current_target(origin)
    rows = await asyncio.to_thread(_list_open_sync, origin if origin else None, target)
    if not rows:
        return "当前没有未完成的待办"
    lines = [f"#{r['id']} {r['text']}{_fmt_due(r['due_at'])}" for r in rows]
    return "未完成待办：\n" + "\n".join(lines)


async def _cancel_linked_reminder(reminder_id, origin: str | None, target: str | None) -> None:
    """Cancel a todo's linked reminder, scoped + best-effort (a failure here
    must not abort the todo close — log and move on)."""
    if reminder_id is None:
        return
    try:
        await asyncio.to_thread(scheduler._cancel_sync, reminder_id, origin, target)
    except Exception as e:  # pragma: no cover
        logger.warning("failed to cancel reminder #%s for todo: %s", reminder_id, e)


@arcana.tool(
    when_to_use="当用户说某件待办做完了，传入待办编号把它标记为完成",
    what_to_expect="完成确认",
    failure_meaning="该编号不存在、已完成、或不属于当前来源",
    side_effect="write",
)
async def complete_todo(todo_id: int) -> str:
    """按编号把一条待办标记为已完成（并取消它挂着的到期提醒）。"""
    todo_id = scheduler.coerce_int(todo_id, default=-1)
    if todo_id is None or todo_id < 0:
        return "待办编号必须是整数"
    origin = scheduler._current_origin()
    target = scheduler._current_target(origin)
    ok, reminder_id = await asyncio.to_thread(
        _close_sync, todo_id, origin if origin else None, target, delete=False
    )
    if not ok:
        return f"未找到可完成的待办 #{todo_id}"
    await _cancel_linked_reminder(reminder_id, origin if origin else None, target)
    return f"已完成待办 #{todo_id} ✅"


@arcana.tool(
    when_to_use="当用户想删除/取消一条待办（不是完成，而是不要了），传入待办编号",
    what_to_expect="取消确认",
    failure_meaning="该编号不存在或不属于当前来源",
    side_effect="write",
)
async def cancel_todo(todo_id: int) -> str:
    """按编号删除一条待办（并取消它挂着的到期提醒）。"""
    todo_id = scheduler.coerce_int(todo_id, default=-1)
    if todo_id is None or todo_id < 0:
        return "待办编号必须是整数"
    origin = scheduler._current_origin()
    target = scheduler._current_target(origin)
    ok, reminder_id = await asyncio.to_thread(
        _close_sync, todo_id, origin if origin else None, target, delete=True
    )
    if not ok:
        return f"未找到待办 #{todo_id}"
    await _cancel_linked_reminder(reminder_id, origin if origin else None, target)
    return f"已删除待办 #{todo_id}"
