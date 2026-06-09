"""Chat-history full-text search tool.

Lets the agent answer "我们之前聊过 X 吗 / 上次关于 Y 我怎么说的" by searching the
SQLite-backed conversation log instead of only seeing the current turn. Read
-only — backed by the FTS5 index + triggers in chat_store.py.
"""

from __future__ import annotations

import time

import arcana

import chat_store


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
    rows = await chat_store.search_messages(query, limit)
    if not rows:
        return f"没有找到包含「{query}」的历史消息"
    lines = []
    for r in rows:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r.get("created_at") or 0))
        role = {"user": "你", "assistant": "我"}.get(r.get("role"), r.get("role") or "?")
        snip = (r.get("snippet") or "").replace("\n", " ").strip()
        lines.append(f"[{when}] {role}: {snip}")
    return f"「{query}」的历史匹配（{len(rows)} 条）：\n" + "\n".join(lines)
