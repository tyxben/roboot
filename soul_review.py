"""Review gate for `soul.md` overwrites.

Every tool that rewrites `soul.md` (update_self / remember_user / add_note /
the distiller's append_self_feedback) goes through `review_write()` first so
a prompt-injected agent can't silently persist malicious content.

Mode is chosen by the ROBOOT_SOUL_REVIEW env var:
  - off     — current behavior; write proceeds without ceremony (default).
  - log     — write proceeds, but the unified diff is also saved to
              .soul/pending/<ts>-<origin>.diff for after-the-fact audit.
  - confirm — the daemon broadcasts a `soul_review` frame to every
              connected console (local + paired mobile) and awaits a
              `soul_review_decision` reply. No reply within the timeout
              counts as REJECTED. Automated origins (the periodic
              distiller) degrade to LOG so the user isn't modal-spammed
              every 20 turns.

Any diff bigger than MAX_DIFF_BYTES is rejected outright — too large to
review safely over a phone. The agent must make smaller edits or the user
must edit `soul.md` by hand.

Callers:
    decision = await review_write(before, after, origin="update_self")
    if decision in {Decision.AUTO, Decision.APPROVED, Decision.LOGGED}:
        _write_soul(after)

Sync variant for automated / sync code paths (distiller):
    decision = review_write_sync(before, after, origin="distill")
    if decision in {Decision.AUTO, Decision.LOGGED}:
        _write_soul(after)
"""

from __future__ import annotations

import asyncio
import difflib
import logging
import os
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

PENDING_DIR = Path(__file__).parent / ".soul" / "pending"
MAX_DIFF_BYTES = 2048
DEFAULT_TIMEOUT_S = 30.0


class Mode(str, Enum):
    OFF = "off"
    LOG = "log"
    CONFIRM = "confirm"


class Decision(str, Enum):
    AUTO = "auto"          # mode=OFF → write proceeds unreviewed
    LOGGED = "logged"      # mode=LOG (or CONFIRM degraded) → diff saved + write proceeds
    APPROVED = "approved"  # mode=CONFIRM → user clicked allow
    REJECTED = "rejected"  # mode=CONFIRM denied or timed out, or diff too big


def get_mode() -> Mode:
    raw = os.environ.get("ROBOOT_SOUL_REVIEW", "off").strip().lower()
    try:
        return Mode(raw)
    except ValueError:
        logger.warning("unknown ROBOOT_SOUL_REVIEW=%r, defaulting to off", raw)
        return Mode.OFF


def _make_diff(before: str, after: str, origin: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"soul.md (before {origin})",
            tofile=f"soul.md (after {origin})",
            n=3,
        )
    )


def _log_pending(diff: str, origin: str) -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    # Collisions on same-second writes are possible; add a short suffix.
    path = PENDING_DIR / f"{ts}-{uuid.uuid4().hex[:6]}-{origin}.diff"
    path.write_text(diff)
    return path


# Pluggable broadcasters. `server.py` registers one that fans a frame to every
# active local WS; `relay_client.py` registers another that fans to every
# paired mobile client. Broadcasters must be async.
_broadcasters: list[Callable[[dict], Awaitable[None]]] = []
# req_id -> future that resolves with True/False when a client replies.
_pending: dict[str, asyncio.Future] = {}


def register_broadcaster(fn: Callable[[dict], Awaitable[None]]) -> None:
    """Register an async callable that ships a frame to all connected clients."""
    if fn not in _broadcasters:
        _broadcasters.append(fn)


def unregister_broadcaster(fn: Callable[[dict], Awaitable[None]]) -> None:
    """Test hook — remove a previously-registered broadcaster."""
    try:
        _broadcasters.remove(fn)
    except ValueError:
        pass


def resolve_decision(req_id: str, approved: bool) -> bool:
    """Called by WS handlers on receipt of a `soul_review_decision` frame.

    Returns True if a pending review was actually resolved. False means the
    review already timed out or doesn't exist (stale click).
    """
    fut = _pending.pop(req_id, None)
    if fut is None or fut.done():
        return False
    fut.set_result(approved)
    return True


async def review_write(
    before: str,
    after: str,
    *,
    origin: str,
    automated: bool = False,
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Decision:
    """Gate a prospective `soul.md` overwrite.

    Returns a Decision; the caller decides whether to actually write based
    on that. `automated=True` forces CONFIRM to degrade to LOG so periodic
    writes (distillation, self-feedback) never pop a modal.
    """
    if before == after:
        return Decision.AUTO

    mode = get_mode()
    if mode == Mode.OFF:
        return Decision.AUTO

    diff = _make_diff(before, after, origin)
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        logger.warning(
            "soul_review: diff exceeds %d bytes (origin=%s), rejecting",
            MAX_DIFF_BYTES,
            origin,
        )
        _log_pending(diff, f"{origin}-REJECTED-OVERSIZE")
        return Decision.REJECTED

    if automated or mode == Mode.LOG:
        _log_pending(diff, origin)
        return Decision.LOGGED

    # mode == CONFIRM, interactive write.
    if not _broadcasters:
        logger.warning("soul_review: no broadcasters registered, degrading to LOG")
        _log_pending(diff, origin)
        return Decision.LOGGED

    req_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _pending[req_id] = fut

    frame = {
        "type": "soul_review",
        "req_id": req_id,
        "origin": origin,
        "diff": diff,
        "timeout_s": timeout,
    }
    for bc in list(_broadcasters):
        try:
            await bc(frame)
        except Exception as e:
            logger.warning("soul_review broadcaster failed: %s", e)

    try:
        approved = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _pending.pop(req_id, None)
        _log_pending(diff, f"{origin}-TIMEOUT")
        return Decision.REJECTED

    if approved:
        return Decision.APPROVED
    _log_pending(diff, f"{origin}-REJECTED")
    return Decision.REJECTED


def review_write_sync(
    before: str,
    after: str,
    *,
    origin: str,
) -> Decision:
    """Synchronous variant for automated / non-async callers (distiller).

    No broadcast path — CONFIRM degrades to LOG here by construction, since
    we can't await a response from sync context. Callers that care about
    interactive review must use the async `review_write`.
    """
    if before == after:
        return Decision.AUTO

    mode = get_mode()
    if mode == Mode.OFF:
        return Decision.AUTO

    diff = _make_diff(before, after, origin)
    if len(diff.encode("utf-8")) > MAX_DIFF_BYTES:
        logger.warning(
            "soul_review: diff exceeds %d bytes (origin=%s), rejecting",
            MAX_DIFF_BYTES,
            origin,
        )
        _log_pending(diff, f"{origin}-REJECTED-OVERSIZE")
        return Decision.REJECTED

    _log_pending(diff, origin)
    return Decision.LOGGED
