"""Chat-history full-text search tool.

Lets the agent answer "我们之前聊过 X 吗 / 上次关于 Y 我怎么说的" by searching the
SQLite-backed conversation log instead of only seeing the current turn. Read
-only — backed by the FTS5 index + triggers in chat_store.py.
"""

import time

import arcana

import chat_store
from tools import scheduler

# How a caller's origin (tool_guard.current_origin) maps to the session
# `source` stamped at write time. NOTE the deliberate mismatch: the relay
# surface's contextvar is "relay" but its sessions are stored source="remote"
# (relay_client.py). A naive source==origin filter would return zero rows for
# relay clients, so map it here.
_ORIGIN_TO_SOURCE = {"local": "local", "relay": "remote", "telegram": "telegram"}


def _scope() -> tuple[str, str | None]:
    """Return (source, label) restricting search to the caller's own history.
    Fails closed: an unknown origin, or a Telegram turn with no user id, maps
    to an impossible source/label so the search returns nothing rather than
    leaking another surface's transcripts."""
    origin = scheduler._current_origin()
    source = _ORIGIN_TO_SOURCE.get(origin)
    if source is None:
        return "\x00none", None  # unknown origin → match nothing
    if source == "telegram":
        target = scheduler._current_target(origin)  # the telegram user_id
        return source, (target if target is not None else "\x00no-user")
    # local / relay: scope by surface; relay isn't per-client today (no
    # client_id contextvar) — all paired clients share the pairing trust.
    return source, None


@arcana.tool(
    when_to_use=(
        "当用户问起过去的对话内容时，例如'我们之前聊过…吗'、'上次我说的那个…'、"
        "'我什么时候提过…'。在全部历史聊天记录里做全文检索。"
    ),
    what_to_expect="匹配到的历史消息片段列表（时间、角色、内容摘要）",
    failure_meaning="没有匹配结果，或检索失败",
    side_effect="read",
)
async def search_chat(query: str, limit: int = 10) -> str:
    """在全部历史聊天记录中全文搜索 query，返回最相关的若干条。"""
    query = (query or "").strip()
    if not query:
        return "搜索词不能为空"
    source, label = _scope()
    rows = await chat_store.search_messages(query, limit, source=source, label=label)
    if not rows:
        return f"没有找到包含「{query}」的历史消息"
    lines = []
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("created_at") or 0))
        role = {"user": "你", "assistant": "我"}.get(r.get("role"), r.get("role") or "?")
        snip = (r.get("snippet") or "").replace("\n", " ").strip()
        lines.append(f"[{when}] {role}: {snip}")
    return f"「{query}」的历史匹配（{len(rows)} 条）：\n" + "\n".join(lines)
