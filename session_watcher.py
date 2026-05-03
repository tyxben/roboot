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
TAIL_LINES = 10

Subscriber = Callable[[dict], Awaitable[None]]

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")
# Idempotent HTML escape: skips `&` when it already starts a known entity
# so sanitize(sanitize(x)) == sanitize(x).
_AMP_RE = re.compile(r"&(?!(?:amp|lt|gt);)")
_PROMPT_LINE_MAX = 200


# Prompt-injection mitigation: terminal tails are untrusted, so strip ANSI /
# control chars, HTML-escape, collapse whitespace, and cap length.
def _sanitize_prompt_line(line: str) -> str:
    if not line:
        return ""
    cleaned = _ANSI_RE.sub("", line)
    # Keep printables plus whitespace (\t, \n, \r) so the whitespace
    # collapse below can squash them into single spaces.
    cleaned = "".join(
        ch
        for ch in cleaned
        if ch in ("\t", "\n", "\r") or (ord(ch) >= 32 and ord(ch) != 127)
    )
    cleaned = _AMP_RE.sub("&amp;", cleaned)
    cleaned = cleaned.replace("<", "&lt;").replace(">", "&gt;")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) > _PROMPT_LINE_MAX:
        # Avoid cutting in the middle of an entity like `&amp;`.
        truncated = cleaned[:_PROMPT_LINE_MAX]
        m = re.search(r"&(?:amp|lt|gt)?;?$", truncated)
        if m and ";" not in m.group(0):
            truncated = truncated[: m.start()]
        cleaned = truncated + "…"
    return cleaned


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
        # Subscribers fired on the *set* of session ids changing (add or
        # remove). Distinct from `_subscribers` which only fires on
        # idle->waiting prompt transitions.
        self._sessions_changed_subscribers: list[Callable[[], Awaitable[None]]] = []
        # None = uninitialized; first successful poll seeds it without
        # broadcasting, so a daemon restart does not look like "all sessions
        # appeared". Subsequent polls compare against this set.
        self._prev_session_ids: set[str] | None = None
        self._task: asyncio.Task | None = None
        self._running = False

    # --- Subscription API ---

    def subscribe(self, callback: Subscriber) -> None:
        """Register an async callback invoked on every idle->waiting transition.

        Callback receives {"session_id", "project", "prompt_line"}. Exceptions
        raised by the callback are caught and logged.
        """
        self._subscribers.append(callback)

    def subscribe_sessions_changed(
        self, callback: Callable[[], Awaitable[None]]
    ) -> None:
        """Register an async callback invoked when the set of iTerm2 session
        ids changes between polls (a window/tab is opened or closed).

        Callback takes no arguments — subscribers re-fetch the full list
        themselves. Exceptions are caught and logged. The first successful
        poll does NOT fire (initial state, not a change).
        """
        self._sessions_changed_subscribers.append(callback)

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
            # Swallow and bail without touching `_prev_session_ids` — a
            # transient iTerm2 hiccup must not look like "every session
            # disappeared" on the next successful poll.
            logger.debug("[watcher] list_sessions failed: %s", e)
            return

        current_ids: set[str] = {s.session_id for s in sessions}
        if self._prev_session_ids is None:
            # First successful poll: seed silently. Daemon restart should
            # not cause a spurious "everything new" broadcast.
            self._prev_session_ids = current_ids
        elif current_ids != self._prev_session_ids:
            self._prev_session_ids = current_ids
            await self._notify_sessions_changed()

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
        # Sanitize before fanning out — subscribers forward this into
        # notify frames / toasts / LLM-visible memory.
        if "prompt_line" in payload:
            payload["prompt_line"] = _sanitize_prompt_line(payload.get("prompt_line") or "")
        for cb in list(self._subscribers):
            try:
                await cb(payload)
            except Exception as e:
                logger.warning("[watcher] subscriber %r failed: %s", cb, e)

    async def _notify_sessions_changed(self) -> None:
        """Fan out to every sessions-changed subscriber, isolating failures."""
        for cb in list(self._sessions_changed_subscribers):
            try:
                await cb()
            except Exception as e:
                logger.warning("[watcher] sessions_changed subscriber %r failed: %s", cb, e)


# Module-level singleton used by server.py + adapters.telegram_bot.
watcher = SessionWatcher()
