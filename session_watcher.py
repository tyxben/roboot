"""Proactive session-waiting notifier.

Background task that polls iTerm2 sessions via the existing `iterm_bridge`
and fires async subscriber callbacks exactly once per idle -> waiting
transition when a Claude Code session enters a "waiting for confirmation"
state.

Design notes
------------
- Per-session state: {session_id: "idle" | "waiting"}. Notification is only
  emitted when a session transitions idle -> waiting. When the prompt
  disappears (no pattern match in the tail), the state flips back to idle
  and the next idle -> waiting transition will fire again.
- State is in-memory only; daemon restart means the first post-restart hit
  will notify once even if the prompt has been lingering. This is
  intentional — we'd rather re-notify after a restart than miss a prompt.
- Any exception from iTerm2 reads is swallowed and the loop keeps running.
  iTerm2 being unreachable must never kill the server.
- Subscribers are async callables taking a single dict payload:
    {"session_id": str, "project": str, "prompt_line": str}
  Individual subscriber failures are logged but never break the loop or
  prevent other subscribers from running.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# Port of the simplest CONFIRM_PATTERNS entries from relay/src/pair-page.ts.
# We only ship a couple — the watcher is a best-effort "heads up" signal,
# not the ground truth; the mobile pair-page keeps the full list.
CONFIRM_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"Do you want to proceed", re.IGNORECASE),
    re.compile(r"\[y/n\]", re.IGNORECASE),
]

POLL_INTERVAL_SECONDS = 5
TAIL_LINES = 20

Subscriber = Callable[[dict], Awaitable[None]]


def _match_prompt_line(tail: str) -> str | None:
    """Return the most recent line matching any CONFIRM_PATTERN, or None."""
    if not tail:
        return None
    lines = tail.split("\n")
    for line in reversed(lines):
        for pat in CONFIRM_PATTERNS:
            if pat.search(line):
                return line.strip()
    return None


class SessionWatcher:
    """Polls iTerm2 sessions and notifies subscribers on idle->waiting."""

    def __init__(
        self,
        *,
        poll_interval: float = POLL_INTERVAL_SECONDS,
        tail_lines: int = TAIL_LINES,
    ):
        self._poll_interval = poll_interval
        self._tail_lines = tail_lines
        # session_id -> "idle" | "waiting"
        self._states: dict[str, str] = {}
        self._subscribers: list[Subscriber] = []
        self._task: asyncio.Task | None = None
        self._running = False

    # --- Subscription API ---

    def subscribe(self, callback: Subscriber) -> None:
        """Register an async callback invoked on every idle->waiting transition.

        Callback receives {"session_id", "project", "prompt_line"}. Exceptions
        raised by the callback are caught and logged.
        """
        self._subscribers.append(callback)

    # --- Lifecycle ---

    def start(self) -> asyncio.Task:
        """Start the background polling task. Idempotent."""
        if self._task is not None and not self._task.done():
            return self._task
        self._running = True
        self._task = asyncio.create_task(self._run(), name="session-watcher")
        return self._task

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    # --- Core loop ---

    async def _run(self) -> None:
        while self._running:
            try:
                await self.poll_once()
            except Exception as e:  # pragma: no cover - defensive
                logger.warning("[watcher] poll cycle failed: %s", e)
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                break

    async def poll_once(self) -> None:
        """Single pass: list sessions, read tails, update state machine.

        Exposed for tests to drive the state machine deterministically.
        """
        # Lazy import so importing session_watcher in a no-iterm2 test env
        # (or on startup before iterm2 is available) doesn't blow up.
        try:
            from iterm_bridge import bridge
        except Exception as e:
            logger.debug("[watcher] iterm_bridge import failed: %s", e)
            return

        try:
            sessions = await bridge.list_sessions()
        except Exception as e:
            logger.debug("[watcher] list_sessions failed: %s", e)
            return

        seen_ids: set[str] = set()
        for s in sessions:
            seen_ids.add(s.session_id)
            try:
                tail = await bridge.read_session(s.session_id, num_lines=self._tail_lines)
            except Exception as e:
                logger.debug("[watcher] read_session %s failed: %s", s.session_id, e)
                continue

            prompt_line = _match_prompt_line(tail or "")
            prev = self._states.get(s.session_id, "idle")

            if prompt_line is not None:
                if prev != "waiting":
                    # idle -> waiting transition: notify exactly once.
                    self._states[s.session_id] = "waiting"
                    await self._notify(
                        {
                            "session_id": s.session_id,
                            "project": s.project,
                            "prompt_line": prompt_line,
                        }
                    )
                # else: still waiting -> do NOT notify again.
            else:
                # Prompt gone: reset to idle so future transitions fire again.
                if prev != "idle":
                    self._states[s.session_id] = "idle"

        # Forget sessions that no longer exist so a re-created session_id
        # starts fresh.
        stale = [sid for sid in self._states if sid not in seen_ids]
        for sid in stale:
            self._states.pop(sid, None)

    async def _notify(self, payload: dict) -> None:
        for cb in list(self._subscribers):
            try:
                await cb(payload)
            except Exception as e:
                logger.warning("[watcher] subscriber %r failed: %s", cb, e)


# Module-level singleton used by server.py + adapters.telegram_bot.
watcher = SessionWatcher()
