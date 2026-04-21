"""Conversation memory: short-term replay + long-term distillation.

Why this module exists
----------------------
Before this, each WebSocket (re)connect created a fresh `ChatSession` with
an empty history. A user hopping off Wi-Fi, refreshing the browser, or
switching between the local console and the relay pair page got a blank-
slate assistant that had forgotten the last five turns. The transcript was
already persisted by `chat_store`; nothing was replaying it.

Two layers are implemented here:

Layer A -- short-term replay (`replay_history`)
    On `create_chat_session`, fetch the last N messages from chat_store and
    seed them into the Arcana `ChatSession` so the next turn has context.

Layer B -- long-term distillation (`TurnCounter` + `maybe_distill`)
    After every K completed turns we kick off a background task that asks
    the agent (throwaway session) to produce a short "here's what we just
    learned about the user" paragraph and appends it to soul.md's
    About User section via `tools.soul.remember_user`. Idempotent sentinel
    "NOTHING" is honored so boring stretches don't pollute soul.md.

Arcana API notes (verified at runtime against arcana-agent 0.4.x)
-----------------------------------------------------------------
`ChatSession` has no public `add_message` / `history.extend` seeding
method. The `history` property returns a **copy** of non-system messages
as dicts, so `session.history.append(...)` is a no-op.

The actual backing list is `session._messages: list[Message]`, starting
with the system prompt at index 0. Public seeding API: absent. We append
`arcana.runtime.conversation.Message(role=MessageRole.USER/ASSISTANT,
content=...)` to `_messages` directly. This is a stable internal — Arcana
exposes `Message`, `MessageRole`, and `ChatSession.history` in its public
surface, just not a combined seeding entrypoint.

If a future Arcana version exposes a seeding API, `_seed_messages` is the
only place to update. If `_messages` disappears entirely, `replay_history`
falls back to injecting a single synthetic `user` message summarizing the
prior turns — so the feature degrades gracefully instead of blowing up.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Any, Awaitable, Callable

import chat_store

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Layer A -- short-term replay
# -----------------------------------------------------------------------------

# Default replay window. Short enough to stay cheap on token counts, long
# enough to cover "where were we" after a browser refresh. Adjust via the
# `n` parameter if a caller wants different semantics.
DEFAULT_REPLAY_N = 10


def _seed_messages(session: Any, messages: list[dict]) -> int:
    """Inject prior-turn messages into a ChatSession.

    Returns the number of messages successfully seeded. Returns 0 if the
    Arcana API shape is unrecognized — callers can fall back to a synthetic
    context message in that case.

    Accepts each message as a dict with at least `role` ('user'/'assistant')
    and `content` (str). Unknown roles are skipped quietly (tool turns from
    chat_store, if any, don't belong in a cold-start replay since their
    tool_call_id won't match anything).
    """
    if not messages:
        return 0

    try:
        from arcana.runtime.conversation import Message, MessageRole  # type: ignore
    except Exception as e:  # pragma: no cover — Arcana always present in prod
        logger.warning("Arcana Message import failed: %s", e)
        return 0

    role_map = {
        "user": MessageRole.USER,
        "assistant": MessageRole.ASSISTANT,
    }

    # Resolve the backing message list. Prefer a public attribute if
    # present; fall back to the private `_messages` that Arcana 0.4.x uses.
    backing = None
    if hasattr(session, "_messages") and isinstance(session._messages, list):
        backing = session._messages
    elif hasattr(session, "messages") and isinstance(
        getattr(session, "messages"), list
    ):
        backing = session.messages  # hypothetical future API

    if backing is None:
        logger.warning(
            "ChatSession has no reachable message list; history replay skipped"
        )
        return 0

    seeded = 0
    for m in messages:
        role = role_map.get((m.get("role") or "").lower())
        content = m.get("content") or ""
        if role is None or not content:
            continue
        try:
            backing.append(Message(role=role, content=content))
            seeded += 1
        except Exception as e:
            logger.warning("Failed to seed one message into ChatSession: %s", e)
            continue
    return seeded


def _fallback_context_summary(messages: list[dict]) -> str | None:
    """Produce a single synthetic 'here's what happened earlier' string.

    Used only when `_seed_messages` can't reach the ChatSession's backing
    list (unlikely in prod, but keeps the feature robust). The returned
    string is intended to be fed as one synthetic user turn, wrapped in a
    `[Context from earlier: ...]` prefix so the model treats it as context
    rather than a user instruction.
    """
    if not messages:
        return None
    lines = []
    for m in messages:
        role = (m.get("role") or "").lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            lines.append(f"User: {content}")
        elif role == "assistant":
            # Trim assistant turns hard — replays shouldn't blow the
            # context window. 200 chars keeps flavor without bloat.
            snippet = content if len(content) <= 200 else content[:200] + "..."
            lines.append(f"Assistant: {snippet}")
    if not lines:
        return None
    joined = "\n".join(lines)
    return f"[Context from earlier in this conversation:\n{joined}\n]"


async def replay_history(
    session: Any,
    history_session_id: str | None,
    n: int = DEFAULT_REPLAY_N,
) -> int:
    """Seed the last N messages from chat_store into a fresh ChatSession.

    Args:
        session: Arcana ChatSession (or a stand-in with `_messages` list).
        history_session_id: chat_store session id. If falsy, no-op.
        n: number of most-recent messages to replay.

    Returns the number of messages actually seeded. Safe to call on a
    brand-new chat_store session with no messages — returns 0 without
    mutating the session.
    """
    if not history_session_id or n <= 0:
        return 0

    try:
        messages = await chat_store.list_messages(history_session_id, limit=n)
    except Exception as e:
        logger.warning("replay_history: chat_store fetch failed: %s", e)
        return 0

    if not messages:
        return 0

    seeded = _seed_messages(session, messages)
    if seeded > 0:
        logger.info("replay_history: seeded %d prior messages", seeded)
        return seeded

    # Fallback path: inject a synthetic context blob as one user turn. This
    # only runs if _seed_messages couldn't find a backing list.
    summary = _fallback_context_summary(messages)
    if not summary:
        return 0
    try:
        from arcana.runtime.conversation import Message, MessageRole  # type: ignore

        backing = getattr(session, "_messages", None)
        if isinstance(backing, list):
            backing.append(Message(role=MessageRole.USER, content=summary))
            logger.info("replay_history: seeded synthetic context fallback")
            return 1
    except Exception as e:
        logger.warning("replay_history: fallback seeding failed: %s", e)
    return 0


# -----------------------------------------------------------------------------
# Layer B -- long-term distillation every K turns
# -----------------------------------------------------------------------------

DISTILL_EVERY_K = 20  # Fire distillation after this many turns per session.
DISTILL_NOTHING_SENTINEL = "NOTHING"
DISTILL_MIN_LEN = 20  # Shorter outputs are treated as no-op (stray whitespace etc.)


class TurnCounter:
    """Per-session turn counter. Cheap dict-backed tracking.

    Keyed by whatever the caller wants — chat_store session_id, telegram
    user_id, a relay client_id, etc. Lives in-process; resets on restart,
    which is fine because distillation is best-effort.
    """

    def __init__(self, every_k: int = DISTILL_EVERY_K):
        self.every_k = every_k
        self._counts: dict[Any, int] = {}

    def bump(self, key: Any) -> int:
        """Increment and return the new count for `key`."""
        self._counts[key] = self._counts.get(key, 0) + 1
        return self._counts[key]

    def should_distill(self, key: Any) -> bool:
        """True iff the counter for `key` just hit `every_k`."""
        return self._counts.get(key, 0) >= self.every_k

    def reset(self, key: Any) -> None:
        self._counts.pop(key, None)

    def get(self, key: Any) -> int:
        return self._counts.get(key, 0)


# Module-level counter so all adapters share one. Adapters pass their own
# key namespace (e.g. client_id, telegram user_id, history_session_id) to
# avoid collisions.
turn_counter = TurnCounter()


_DISTILL_PROMPT = (
    "你是一个用户建模助手。根据下面最近的对话记录，"
    "抽取我们关于用户的新了解——偏好、身份信息、正在做的项目、"
    "习惯、讨厌/喜欢的东西。只输出**新信息**，一段话，最多三句。"
    "不要重复已经记下过的东西。如果没有值得记的新信息，"
    "只输出 NOTHING 四个大写字母，别的什么都不要写。"
)


def _format_transcript(messages: list[dict]) -> str:
    """Turn a list of chat_store message dicts into a plain transcript."""
    lines = []
    for m in messages:
        role = (m.get("role") or "").lower()
        content = (m.get("content") or "").strip()
        if not content:
            continue
        tag = {"user": "用户", "assistant": "助手"}.get(role, role)
        lines.append(f"{tag}: {content}")
    return "\n".join(lines)


DistillRunner = Callable[[str, str], Awaitable[str]]
"""Async callable that takes (system_prompt, user_text) and returns the
model's reply. Abstracted so tests can inject a deterministic fake without
spinning up a real Arcana runtime."""


async def _default_distill_runner(
    runtime: Any, system_prompt: str, user_text: str
) -> str:
    """Run a one-shot throwaway ChatSession and return its reply content."""
    session = runtime.create_chat_session(system_prompt=system_prompt)
    resp = await session.send(user_text)
    # Arcana's .send() returns an object with a `.content` attribute.
    content = getattr(resp, "content", None)
    if content is None and isinstance(resp, str):
        content = resp
    return content or ""


async def distill_and_record(
    history_session_id: str,
    *,
    runtime: Any,
    remember_user_fn: Callable[[str], Awaitable[str]] | None = None,
    runner: DistillRunner | None = None,
    k: int = DISTILL_EVERY_K,
) -> str | None:
    """Run one distillation pass.

    Fetches the last `k` messages, asks the model for a delta, and (if
    non-trivial) appends it to soul.md's About User via remember_user.

    Returns the delta string that was recorded, or None if distillation
    produced NOTHING / short output / errored.

    Broken out as a standalone coroutine so the counter path just needs to
    `asyncio.create_task(distill_and_record(...))`.
    """
    if remember_user_fn is None:
        from tools.soul import remember_user as _rem  # lazy to keep tests cheap

        async def remember_user_fn_impl(fact: str) -> str:
            # remember_user is an @arcana.tool; calling the wrapped function
            # directly gets the underlying coroutine.
            inner = getattr(_rem, "__wrapped__", _rem)
            return await inner(fact)

        remember_user_fn = remember_user_fn_impl

    try:
        messages = await chat_store.list_messages(history_session_id, limit=k)
    except Exception as e:
        logger.warning("distill: fetch messages failed: %s", e)
        return None
    if not messages:
        return None

    transcript = _format_transcript(messages)
    if not transcript.strip():
        return None

    run = runner or (lambda sp, ut: _default_distill_runner(runtime, sp, ut))

    try:
        reply = await run(_DISTILL_PROMPT, transcript)
    except Exception as e:
        logger.warning("distill: runner failed: %s", e)
        return None

    reply = (reply or "").strip()
    # Strip any leading/trailing quoting the model might wrap around its
    # answer.
    reply = re.sub(r"^[`'\"]+|[`'\"]+$", "", reply).strip()

    if (
        not reply
        or len(reply) < DISTILL_MIN_LEN
        or DISTILL_NOTHING_SENTINEL in reply.upper()
    ):
        logger.info("distill: no new user knowledge this window")
        return None

    try:
        await remember_user_fn(reply)
    except Exception as e:
        logger.warning("distill: remember_user failed: %s", e)
        return None

    logger.info("distill: appended delta to soul.md (%d chars)", len(reply))
    return reply


# -----------------------------------------------------------------------------
# Layer B' -- self-feedback distillation (sibling of user-knowledge distill)
# -----------------------------------------------------------------------------

# Prompt intentionally demands specificity. The user has been burned by
# vague self-help slop ("I should be more helpful") and sycophantic
# no-ops ("everything went great!") — reject both explicitly.
_SELF_FEEDBACK_PROMPT = (
    "回顾下面最近 20 轮对话。找出**我**（助手）做过让用户纠正、反感、"
    "不得不重复要求、或者明显不满的具体时刻。输出一段话，不超过 60 字，"
    "写给未来的自己看，要**具体**——指出做错的那件事和应该怎么改。"
    "不要说空话如“我应该更有帮助”或“我要更主动”。"
    "不要自夸如“整体表现不错”或“用户很满意”。"
    "如果这 20 轮里没有用户的纠正或不满，只输出 NOTHING 四个大写字母，"
    "别的什么都不要写。"
)


def _build_self_feedback_prompt_user_text(transcript: str) -> str:
    """User-side payload for the self-feedback distiller.

    Kept as a function (not inline) so tests can assert the transcript is
    present in the prompt context.
    """
    return f"最近 20 轮对话：\n\n{transcript}"


async def distill_self_feedback(
    history_session_id: str,
    *,
    runtime: Any,
    append_fn: Callable[[str], None] | None = None,
    runner: DistillRunner | None = None,
    k: int = DISTILL_EVERY_K,
) -> str | None:
    """Run one self-feedback distillation pass.

    Mirrors `distill_and_record` but writes to soul.md's `## 自我反馈` via
    `tools.soul.append_self_feedback` instead of the About-User section.

    Returns the feedback line that was recorded, or None if the model
    produced NOTHING / short output / errored.
    """
    if append_fn is None:
        from tools.soul import append_self_feedback as _append  # lazy import

        def append_fn_impl(text: str) -> None:
            _append(text)

        append_fn = append_fn_impl

    try:
        messages = await chat_store.list_messages(history_session_id, limit=k)
    except Exception as e:
        logger.warning("self-feedback: fetch messages failed: %s", e)
        return None
    if not messages:
        return None

    transcript = _format_transcript(messages)
    if not transcript.strip():
        return None

    user_text = _build_self_feedback_prompt_user_text(transcript)
    run = runner or (lambda sp, ut: _default_distill_runner(runtime, sp, ut))

    try:
        reply = await run(_SELF_FEEDBACK_PROMPT, user_text)
    except Exception as e:
        logger.warning("self-feedback: runner failed: %s", e)
        return None

    reply = (reply or "").strip()
    reply = re.sub(r"^[`'\"]+|[`'\"]+$", "", reply).strip()

    if (
        not reply
        or len(reply) < DISTILL_MIN_LEN
        or DISTILL_NOTHING_SENTINEL in reply.upper()
    ):
        logger.info("self-feedback: nothing substantive this window")
        return None

    try:
        append_fn(reply)
    except Exception as e:
        logger.warning("self-feedback: append failed: %s", e)
        return None

    logger.info("self-feedback: appended note to soul.md (%d chars)", len(reply))
    return reply


async def _run_both_distillations(
    history_session_id: str, *, runtime: Any, k: int
) -> None:
    """Fire user-knowledge and self-feedback distillations concurrently.

    Best-effort: each coroutine already swallows its own exceptions, but we
    wrap with `return_exceptions=True` defensively so a surprise bubble-up
    from one never aborts the other.
    """
    await asyncio.gather(
        distill_and_record(history_session_id, runtime=runtime, k=k),
        distill_self_feedback(history_session_id, runtime=runtime, k=k),
        return_exceptions=True,
    )


def record_turn_and_maybe_distill(
    history_session_id: str | None,
    *,
    runtime: Any,
    counter: TurnCounter | None = None,
    every_k: int | None = None,
) -> asyncio.Task | None:
    """Increment the turn counter and fire distillation tasks if it hit K.

    Schedules both the user-knowledge distillation and the self-feedback
    distillation in a single task group. Both are best-effort; one failing
    must not block the other.

    Returns the asyncio.Task if distillations were scheduled, else None.
    Non-blocking: the caller can call this at the end of each chat turn
    and proceed without awaiting.
    """
    if not history_session_id:
        return None
    c = counter or turn_counter
    if every_k is not None:
        c.every_k = every_k
    count = c.bump(history_session_id)
    if count < c.every_k:
        return None
    # Reset before scheduling so two back-to-back turns don't stack.
    c.reset(history_session_id)
    try:
        return asyncio.create_task(
            _run_both_distillations(
                history_session_id, runtime=runtime, k=c.every_k
            )
        )
    except RuntimeError:
        # No running event loop — happens in edge cases (shutdown, tests).
        return None
