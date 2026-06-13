"""Proactive daily briefing — the first "agent acts on its own clock" feature.

At a configured local time each day, run a headless agent turn that pulls a
briefing (e.g. via the `messageinfra.get_briefing` MCP tool) and pushes it to
connected surfaces. It reuses `chat_handler.handle_chat`, so the brief renders
exactly like a normal streamed agent message — the caller supplies a `send`
callback that fans the frames out to whoever is connected.

Design notes:
- Runs in the SERVER's main event loop (started from the startup event), the
  same loop the MCP stdio transports live in — so the agent can actually call
  `messageinfra.get_briefing`.
- A separate ChatSession per run (independent of any live user session), so a
  brief firing mid-conversation doesn't clobber the user's context.
- If MCP isn't connected the agent simply lacks the tool and says so — graceful.
- If nobody is connected the brief still runs but reaches no surface; a
  persistent inbox is a later Phase-2 item.
- Cancellation (shutdown) ends the loop via the normal asyncio.sleep cancel.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import logging
from typing import Any, AsyncContextManager, Awaitable, Callable

import tool_guard

logger = logging.getLogger(__name__)

Send = Callable[[dict], Awaitable[None]]
InFlight = Callable[[], AsyncContextManager]

# tool_guard origin for the briefing turn. It's an AUTONOMOUS origin
# (tool_guard._AUTONOMOUS_ORIGINS) — the gate forces the turn READ-ONLY, so an
# injected brief can't chain into shell / writes even with the gate off.
BRIEFING_ORIGIN = "briefing"

DEFAULT_BRIEFING_PROMPT = (
    "现在是每日简报时间。请用 messageinfra 的工具（如 get_briefing）拉取今天的"
    "简报/动态，整理成简洁的中文要点。开头用一行 `> ` 引用给出一句话口播总结，"
    "其余写成要点列表。如果相关工具不可用，就直接说明暂时拿不到简报，不要编造。"
)


def seconds_until_daily(hhmm: str, now: datetime.datetime | None = None) -> float:
    """Seconds from `now` until the next local occurrence of HH:MM.

    `now` is injectable for tests. Raises ValueError on a malformed time
    string (caller disables the loop rather than crashing).
    """
    now = now or datetime.datetime.now()
    parts = hhmm.strip().split(":")
    if len(parts) != 2:
        raise ValueError(f"briefing time must be HH:MM, got {hhmm!r}")
    hh, mm = int(parts[0]), int(parts[1])
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"briefing time out of range: {hhmm!r}")
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if target <= now:
        target += datetime.timedelta(days=1)
    return (target - now).total_seconds()


async def run_briefing_once(
    runtime: Any,
    prompt: str,
    push: Send,
    *,
    build_personality: Callable[[], str],
    origin: str = BRIEFING_ORIGIN,
    in_flight: InFlight | None = None,
) -> str:
    """Run one briefing turn through the agent and push the result as ONE
    `response` frame via `push`. Returns the brief text ("" on failure).

    Why a single frame, not per-delta streaming: the brief runs in the server's
    main loop, not the relay's, so per-delta fan-out would reorder on the relay
    and could clobber an in-flight user turn's shared stream bubble. A single
    `response` frame is a self-contained bubble — safe either way.

    The turn runs under tool_guard `origin` (default "briefing"), an AUTONOMOUS
    origin: the gate forces it READ-ONLY, so injected briefing content can't
    chain into shell/writes. `in_flight`, if given, is a no-arg factory
    returning an async context manager — the server uses it to mark the turn
    in-flight so a self-upgrade can't re-exec mid-brief. Never raises (a bad
    brief must not kill the loop)."""
    from chat_handler import handle_chat

    async def _sink(_frame: dict) -> None:
        return None  # swallow streamed frames; we push the final text once

    token = tool_guard.current_origin.set(origin)
    try:
        session = runtime.create_chat_session(system_prompt=build_personality())
        cm = in_flight() if in_flight is not None else contextlib.nullcontext()
        async with cm:
            full_text, _ = await handle_chat(
                session, prompt, _sink, history_session_id=None
            )
    except Exception:
        logger.warning("briefing turn failed", exc_info=True)
        return ""
    finally:
        tool_guard.current_origin.reset(token)

    full_text = (full_text or "").strip()
    if full_text:
        try:
            await push({"type": "response", "content": full_text, "kind": "briefing"})
        except Exception:
            logger.warning("briefing push failed", exc_info=True)
    return full_text


async def briefing_loop(
    runtime: Any,
    *,
    hhmm: str,
    prompt: str,
    push: Send,
    build_personality: Callable[[], str],
    origin: str = BRIEFING_ORIGIN,
    in_flight: InFlight | None = None,
) -> None:
    """Daily loop: sleep until HH:MM, run the brief, repeat. Cancel to stop."""
    while True:
        try:
            delay = seconds_until_daily(hhmm)
        except ValueError:
            logger.warning("briefing: invalid time %r; loop disabled", hhmm)
            return
        await asyncio.sleep(delay)
        logger.info("briefing: firing daily brief")
        await run_briefing_once(
            runtime,
            prompt,
            push,
            build_personality=build_personality,
            origin=origin,
            in_flight=in_flight,
        )
        # Step past the firing minute so the next seconds_until_daily lands on
        # tomorrow, not a re-fire within the same minute.
        await asyncio.sleep(1)
