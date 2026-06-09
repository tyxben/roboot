"""Scheduler / reminder tool — delayed and recurring notifications.

Turns the agent from request-response into something that can act on its own
clock: "15 分钟后提醒我喝水", "1 小时后提醒我看 build", "每天 9 点提醒我吃药".

Design
------
Reminders persist to a SQLite file (`.reminders.db`, gitignored) so they
survive a daemon restart — mirrors chat_store's sync-sqlite-via-to_thread
pattern (autocommit + WAL). The agent tools just read/write rows, so they
work from any process (web console, relay, Telegram).

Delivery is done by a *dispatcher loop* that each long-running process starts
for the surfaces it owns:
  - server.py starts one for origins {local, relay, ""} → fans the reminder
    out to local consoles + paired mobile clients.
  - the Telegram bot starts one for origin {telegram} → DMs the owning user.

Origins are disjoint, so the two dispatchers never deliver the same reminder
twice. As belt-and-suspenders, a reminder is *claimed* with an atomic
`UPDATE ... SET fired=1 WHERE id=? AND fired=0` before delivery; only the
dispatcher whose UPDATE affected a row delivers it.

A new reminder in the same process wakes its dispatcher immediately via an
asyncio.Event; cross-process, the dispatcher's poll (<= POLL_INTERVAL)
picks it up. So the common case ("set from console, fires on console") is
instant.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from pathlib import Path
from typing import Awaitable, Callable

import arcana

logger = logging.getLogger(__name__)

DB_PATH = Path(__file__).parent.parent / ".reminders.db"
POLL_INTERVAL = 20.0  # seconds; cross-process latency ceiling
MAX_DELAY_SECONDS = 365 * 86400  # 1 year — guard against fat-finger/overflow

_INIT_SQL = """
CREATE TABLE IF NOT EXISTS reminders (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  text           TEXT NOT NULL,
  due_at         REAL NOT NULL,
  repeat_seconds REAL NOT NULL DEFAULT 0,
  origin         TEXT NOT NULL DEFAULT '',
  target         TEXT,
  created_at     REAL NOT NULL,
  fired          INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_reminders_due
  ON reminders(fired, due_at);
"""


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(_INIT_SQL)
    return conn


# ---------------------------------------------------------------------------
# Sync DB ops (run via asyncio.to_thread so the loop never blocks on sqlite)
# ---------------------------------------------------------------------------


def _add_sync(
    text: str, due_at: float, repeat_seconds: float, origin: str, target: str | None
) -> int:
    with _connect() as conn:
        cur = conn.execute(
            "INSERT INTO reminders(text, due_at, repeat_seconds, origin, target,"
            " created_at, fired) VALUES (?, ?, ?, ?, ?, ?, 0)",
            (text, due_at, repeat_seconds, origin, target, time.time()),
        )
        return int(cur.lastrowid)


def _list_pending_sync(origins: list[str] | None) -> list[dict]:
    with _connect() as conn:
        if origins:
            qs = ",".join("?" for _ in origins)
            rows = conn.execute(
                f"SELECT id, text, due_at, repeat_seconds, origin, target FROM reminders"
                f" WHERE fired=0 AND origin IN ({qs}) ORDER BY due_at ASC",
                tuple(origins),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, text, due_at, repeat_seconds, origin, target FROM reminders"
                " WHERE fired=0 ORDER BY due_at ASC"
            ).fetchall()
    return [
        {
            "id": r[0],
            "text": r[1],
            "due_at": r[2],
            "repeat_seconds": r[3],
            "origin": r[4],
            "target": r[5],
        }
        for r in rows
    ]


def _cancel_sync(reminder_id: int, origin: str | None) -> bool:
    """Cancel a pending reminder. If `origin` is given, only cancel a reminder
    with that origin — so a Telegram user can't cancel a console reminder by
    guessing its id and vice-versa."""
    with _connect() as conn:
        if origin is not None:
            cur = conn.execute(
                "DELETE FROM reminders WHERE id=? AND fired=0 AND origin=?",
                (reminder_id, origin),
            )
        else:
            cur = conn.execute(
                "DELETE FROM reminders WHERE id=? AND fired=0", (reminder_id,)
            )
        return cur.rowcount > 0


def _claim_due_sync(origins: list[str], now: float) -> list[dict]:
    """Atomically claim due, unfired reminders for the given origins.

    Returns the claimed rows. For recurring reminders the row is rescheduled
    (fired reset to 0, due_at advanced) *after* the claim, so the claiming
    dispatcher owns it exclusively in between."""
    claimed: list[dict] = []
    with _connect() as conn:
        qs = ",".join("?" for _ in origins)
        candidates = conn.execute(
            f"SELECT id, text, due_at, repeat_seconds, origin, target FROM reminders"
            f" WHERE fired=0 AND due_at<=? AND origin IN ({qs})",
            (now, *origins),
        ).fetchall()
        for r in candidates:
            rid, text, due_at, repeat, origin, target = r
            cur = conn.execute(
                "UPDATE reminders SET fired=1 WHERE id=? AND fired=0", (rid,)
            )
            if cur.rowcount != 1:
                continue  # someone else claimed it
            claimed.append(
                {
                    "id": rid,
                    "text": text,
                    "due_at": due_at,
                    "repeat_seconds": repeat,
                    "origin": origin,
                    "target": target,
                }
            )
            if repeat and repeat > 0:
                # Advance to the next occurrence and un-fire so it recurs.
                # Skip any missed slots (e.g. daemon was down) to the next
                # future slot so it doesn't fire a backlog all at once.
                next_due = due_at + repeat
                while next_due <= now:
                    next_due += repeat
                conn.execute(
                    "UPDATE reminders SET fired=0, due_at=? WHERE id=?",
                    (next_due, rid),
                )
    return claimed


def _next_due_at_sync(origins: list[str]) -> float | None:
    with _connect() as conn:
        qs = ",".join("?" for _ in origins)
        row = conn.execute(
            f"SELECT MIN(due_at) FROM reminders WHERE fired=0 AND origin IN ({qs})",
            tuple(origins),
        ).fetchone()
    return row[0] if row and row[0] is not None else None


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

_wake: asyncio.Event | None = None
_dispatcher_task: asyncio.Task | None = None


def _get_wake() -> asyncio.Event:
    global _wake
    if _wake is None:
        _wake = asyncio.Event()
    return _wake


async def _dispatcher_loop(
    origins: list[str], deliver: Callable[[dict], Awaitable[None]]
) -> None:
    wake = _get_wake()
    while True:
        try:
            now = time.time()
            for r in await asyncio.to_thread(_claim_due_sync, origins, now):
                try:
                    await deliver(r)
                except Exception as e:
                    logger.warning("reminder delivery failed (#%s): %s", r["id"], e)
            nxt = await asyncio.to_thread(_next_due_at_sync, origins)
            if nxt is None:
                timeout = POLL_INTERVAL
            else:
                timeout = max(0.0, min(POLL_INTERVAL, nxt - time.time()))
            # Wait until the next due time, or until a freshly-added reminder
            # sets the wake event — whichever is first. Polled in short slices
            # rather than asyncio.wait_for(event.wait()): plain asyncio.sleep
            # cancels cleanly, while wait_for around an Event has cancellation
            # edge-cases that can leave the task un-finalized.
            deadline = time.time() + timeout
            while not wake.is_set():
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                await asyncio.sleep(min(0.25, remaining))
            wake.clear()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("scheduler dispatcher error; backing off")
            await asyncio.sleep(POLL_INTERVAL)


def start_dispatcher(
    origins: list[str], deliver: Callable[[dict], Awaitable[None]]
) -> asyncio.Task:
    """Start (idempotently) the reminder dispatcher for `origins` in the
    current event loop. `deliver(reminder_dict)` is awaited for each due
    reminder. Returns the dispatcher task."""
    global _dispatcher_task
    if _dispatcher_task is not None and not _dispatcher_task.done():
        return _dispatcher_task
    _dispatcher_task = asyncio.create_task(_dispatcher_loop(origins, deliver))
    return _dispatcher_task


async def stop_dispatcher() -> None:
    """Cancel the dispatcher and await its unwind. Mainly for tests / clean
    shutdown — without awaiting the cancelled task, the event loop can't
    finalize cleanly (a dangling task + in-flight to_thread executor)."""
    global _dispatcher_task, _wake
    t = _dispatcher_task
    _dispatcher_task = None
    _wake = None
    if t is not None and not t.done():
        t.cancel()
        try:
            await t
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("scheduler dispatcher raised during shutdown")


def _fmt_when(due_at: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(due_at))


# ---------------------------------------------------------------------------
# Agent tools
# ---------------------------------------------------------------------------


def _current_origin() -> str:
    # Reuse tool_guard's origin contextvar so a reminder remembers which
    # surface created it (and which dispatcher should deliver it).
    try:
        import tool_guard

        return tool_guard.current_origin.get()
    except Exception:
        return ""


def _current_target(origin: str) -> str | None:
    if origin == "telegram":
        try:
            from tools.voice_switch import current_tg_user

            uid = current_tg_user.get(None)
            return str(uid) if uid is not None else None
        except Exception:
            return None
    return None


@arcana.tool(
    when_to_use=(
        "当用户要你在未来某个时间提醒他做某事时，例如'15分钟后提醒我喝水'、"
        "'1小时后提醒我看build'、'每天提醒我吃药'。把时间换算成秒传给 delay_seconds，"
        "周期性提醒再传 repeat_seconds。"
    ),
    what_to_expect="确认信息，包含提醒编号和触发时间",
    failure_meaning="参数不合法（时间为负或过大）或写入失败",
    side_effect="write",
)
async def schedule_reminder(
    text: str, delay_seconds: int, repeat_seconds: int = 0
) -> str:
    """安排一个在 delay_seconds 秒后触发的提醒；repeat_seconds>0 则周期重复。"""
    text = (text or "").strip()
    if not text:
        return "提醒内容不能为空"
    if delay_seconds is None or delay_seconds <= 0:
        return "delay_seconds 必须为正整数（从现在起多少秒后提醒）"
    if delay_seconds > MAX_DELAY_SECONDS:
        return "提醒时间太远了（超过一年）"
    if repeat_seconds and repeat_seconds < 0:
        return "repeat_seconds 不能为负"
    origin = _current_origin()
    target = _current_target(origin)
    due_at = time.time() + float(delay_seconds)
    rid = await asyncio.to_thread(
        _add_sync, text, due_at, float(repeat_seconds or 0), origin, target
    )
    _get_wake().set()  # wake this process's dispatcher if it's the owner
    when = _fmt_when(due_at)
    extra = f"，之后每 {repeat_seconds} 秒重复" if repeat_seconds else ""
    return f"已设置提醒 #{rid}：将在 {when} 提醒你「{text}」{extra}"


@arcana.tool(
    when_to_use="当用户想查看自己设置了哪些待触发的提醒时",
    what_to_expect="待触发提醒的列表（编号、时间、内容）",
    failure_meaning="读取失败",
    side_effect="read",
)
async def list_reminders() -> str:
    """列出当前所有未触发的提醒。"""
    origin = _current_origin()
    # A surface only sees its own reminders, matching how they're delivered.
    origins = [origin] if origin else None
    rows = await asyncio.to_thread(_list_pending_sync, origins)
    if not rows:
        return "当前没有待触发的提醒"
    lines = []
    for r in rows:
        rep = f"（每 {int(r['repeat_seconds'])}s 重复）" if r["repeat_seconds"] else ""
        lines.append(f"#{r['id']} {_fmt_when(r['due_at'])} — {r['text']}{rep}")
    return "待触发提醒：\n" + "\n".join(lines)


@arcana.tool(
    when_to_use="当用户想取消之前设置的某个提醒时，传入提醒编号",
    what_to_expect="取消成功或失败的确认",
    failure_meaning="该编号不存在或不属于当前来源",
    side_effect="write",
)
async def cancel_reminder(reminder_id: int) -> str:
    """按编号取消一个未触发的提醒。"""
    origin = _current_origin()
    ok = await asyncio.to_thread(
        _cancel_sync, reminder_id, origin if origin else None
    )
    return f"已取消提醒 #{reminder_id}" if ok else f"未找到可取消的提醒 #{reminder_id}"
