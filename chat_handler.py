"""Shared chat streaming logic.

Both the local FastAPI WebSocket (`server.py`) and the relay WebSocket
(`relay_client.py`) run the same Arcana streaming loop and emit the same
protocol frames — they only differ in *how* frames reach the client
(plain JSON over TLS vs encrypted envelope over relay). This module
holds the stream loop once; each adapter passes its own `send` callback.

Wire protocol emitted by handle_chat (unchanged from prior duplicated
code, so frontends don't need updates):

  {"type": "thinking"}
  {"type": "delta", "text": "..."}
  {"type": "tool_start", "name": "..."}
  {"type": "tool_end", "name": "..."}
  {"type": "done", "content": "...", "tools_used": N[, "sessions": [...]]}
"""

from __future__ import annotations

from typing import Awaitable, Callable

import chat_store

Send = Callable[[dict], Awaitable[None]]


async def handle_chat(
    session,
    user_text: str,
    send: Send,
    *,
    include_sessions_on_done: bool = True,
    history_session_id: str | None = None,
) -> tuple[str, int]:
    """Run one chat turn, stream frames via `send`, return (full_text, tools_used).

    `include_sessions_on_done` — when True and tools were used, piggyback the
    iTerm2 session list on the final `done` frame. Local ws always wants this;
    the relay sets it False for clients that never opened the sidebar.

    `history_session_id` — when set, the user prompt and assistant reply are
    persisted via chat_store for later retrieval. Adapters pass the id they
    got from chat_store.create_session() at connect time. None disables
    persistence (used by tests / non-persisting code paths).
    """
    user_text = user_text.strip()
    if not user_text:
        return ("", 0)

    if history_session_id:
        await chat_store.record_user(history_session_id, user_text)

    await send({"type": "thinking"})

    full_text = ""
    tools_used = 0

    async for event in session.stream(user_text):
        etype = str(event.event_type)

        if "LLM_CHUNK" in etype and event.content:
            full_text += event.content
            await send({"type": "delta", "text": event.content})

        elif "TOOL_START" in etype or "TOOL_CALL_START" in etype:
            tools_used += 1
            await send({"type": "tool_start", "name": event.tool_name or ""})

        elif "TOOL_END" in etype or "TOOL_RESULT" in etype:
            await send({"type": "tool_end", "name": event.tool_name or ""})

        elif "RUN_COMPLETE" in etype and event.content and not full_text:
            full_text = event.content

    resp: dict = {"type": "done", "content": full_text, "tools_used": tools_used}

    if tools_used > 0 and include_sessions_on_done:
        try:
            from iterm_bridge import bridge

            all_sessions = await bridge.list_sessions()
            resp["sessions"] = [
                {"id": s.session_id, "project": s.project, "name": s.name}
                for s in all_sessions
            ]
        except Exception:
            pass

    await send(resp)

    if history_session_id:
        await chat_store.record_assistant(history_session_id, full_text, tools_used)

    return full_text, tools_used
