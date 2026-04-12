"""Claude Code session management — via iTerm2 Python API."""

from __future__ import annotations

import asyncio
import json
import subprocess

import arcana

from iterm_bridge import bridge


async def _find_session(project_name: str):
    """Find a session by fuzzy matching project name. Returns (SessionInfo, session_id) or None."""
    sessions = await bridge.list_sessions()
    for s in sessions:
        if project_name.lower() in (s.project + s.name + s.cwd).lower():
            return s
    return None


@arcana.tool(
    when_to_use="当用户问'Claude Code 在干嘛'、'有几个会话'、'看看进度'时使用",
    what_to_expect="返回所有 iTerm2 会话列表，包含项目名、session_id、任务描述",
    failure_meaning="iTerm2 未连接",
)
async def list_sessions() -> str:
    """列出所有 iTerm2 会话。"""
    try:
        sessions = await bridge.list_sessions()
        if not sessions:
            return "没有运行中的会话"
        lines = []
        for s in sessions:
            lines.append(f"- **{s.project}** | {s.name} | id={s.session_id[:8]}")
        return f"发现 {len(sessions)} 个会话:\n" + "\n".join(lines)
    except Exception as e:
        return f"查询失败: {e}"


@arcana.tool(
    when_to_use="当需要查看某个会话的最新输出、进度或状态时",
    what_to_expect="该会话的最近终端输出文本",
    failure_meaning="找不到该会话",
)
async def read_session(project_name: str) -> str:
    """读取指定会话的终端输出。通过项目名模糊匹配。"""
    try:
        s = await _find_session(project_name)
        if not s:
            return f"找不到包含 '{project_name}' 的会话"
        content = await bridge.read_session(s.session_id, num_lines=100)
        return f"**{s.project}** ({s.name}) 最近输出:\n\n{content[:6000]}"
    except Exception as e:
        return f"读取失败: {e}"


@arcana.tool(
    when_to_use="当用户说'让那个 Claude Code 去做某事'、'告诉它修一下'、'给它发个指令'、'帮我允许/确认那个操作'时，直接使用此工具发送",
    what_to_expect="文本已发送到指定会话",
    failure_meaning="找不到会话或发送失败",
    side_effect="write",
)
async def send_to_session(project_name: str, text: str) -> str:
    """向指定会话发送文本。通过项目名模糊匹配。"""
    try:
        s = await _find_session(project_name)
        if not s:
            return f"找不到包含 '{project_name}' 的会话"
        result = await bridge.send_text(s.session_id, text)
        if result == "sent":
            return f"已发送到 **{s.project}**。内容: {text[:200]}"
        return f"发送失败: {result}"
    except Exception as e:
        return f"发送失败: {e}"


@arcana.tool(
    when_to_use="当用户想新开一个 Claude Code 来处理任务时",
    what_to_expect="在 iTerm2 中新开一个 tab 并启动 Claude Code",
    failure_meaning="创建失败",
    side_effect="write",
)
async def create_claude_session(directory: str, initial_prompt: str = "") -> str:
    """在 iTerm2 新 tab 中启动 Claude Code。"""
    try:
        result = await bridge.create_session(directory, initial_prompt)
        return result
    except Exception as e:
        return f"创建失败: {e}"
