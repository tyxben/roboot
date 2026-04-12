"""Claude Code session management via tmux."""

from __future__ import annotations

import asyncio
import subprocess

import arcana

CLAUDE_CODE_CMD = "claude"


async def _tmux(cmd: str) -> str:
    """Run a tmux command and return output."""
    proc = await asyncio.create_subprocess_shell(
        f"tmux {cmd}",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=10)
    return (stdout or b"").decode().strip()


@arcana.tool(
    when_to_use="当用户问'Claude Code 在干嘛'、'有几个会话'、'看看进度'时使用",
    what_to_expect="返回所有 tmux 会话列表，包含会话名和状态",
    failure_meaning="tmux 未运行或没有会话",
)
async def list_sessions() -> str:
    """列出所有 tmux 会话（包括 Claude Code 和其他终端会话）。"""
    try:
        output = await _tmux("list-sessions -F '#{session_name} (#{session_windows} windows, created #{session_created_string})'")
        if not output:
            return "没有运行中的 tmux 会话"
        return output
    except Exception:
        return "tmux 未运行。提示：用户需要先在 tmux 中启动 Claude Code 会话"


@arcana.tool(
    when_to_use="当需要查看某个 Claude Code 会话的最新输出、进度或状态时",
    what_to_expect="该会话终端屏幕的最近 N 行文本内容",
    failure_meaning="会话不存在或无法读取",
)
async def read_session(session_name: str, lines: int = 200) -> str:
    """读取指定 tmux 会话的屏幕内容。"""
    try:
        output = await _tmux(
            f"capture-pane -t {session_name} -p -S -{lines}"
        )
        if not output:
            return f"会话 {session_name} 屏幕为空"
        # 去掉大量空行
        cleaned = "\n".join(
            line for line in output.split("\n") if line.strip()
        )
        return cleaned[:6000] or f"会话 {session_name} 无可见内容"
    except Exception as e:
        return f"无法读取会话 {session_name}: {e}"


@arcana.tool(
    when_to_use="当用户说'让那个 Claude Code 去做某事'、'告诉它修一下'、'给它发个指令'时",
    what_to_expect="指令已发送到指定会话，返回确认",
    failure_meaning="会话不存在或发送失败",
    side_effect="write",
)
async def send_to_session(session_name: str, text: str) -> str:
    """向指定 tmux 会话发送文本输入（模拟键盘输入）。"""
    try:
        # 转义特殊字符
        escaped = text.replace("'", "'\\''")
        await _tmux(f"send-keys -t {session_name} '{escaped}' Enter")
        # 等待一下再读取反馈
        await asyncio.sleep(1)
        output = await _tmux(f"capture-pane -t {session_name} -p -S -20")
        return f"已发送到 {session_name}。最新输出:\n{output}"
    except Exception as e:
        return f"发送失败: {e}"


@arcana.tool(
    when_to_use="当用户想新开一个 Claude Code 来处理任务时",
    what_to_expect="新的 tmux 会话已创建并启动了 Claude Code",
    failure_meaning="tmux 创建会话失败",
    side_effect="write",
)
async def create_claude_session(session_name: str, initial_prompt: str = "") -> str:
    """创建新的 tmux 会话并启动 Claude Code。"""
    try:
        cmd = CLAUDE_CODE_CMD
        if initial_prompt:
            escaped = initial_prompt.replace("'", "'\\''")
            cmd = f"{CLAUDE_CODE_CMD} '{escaped}'"

        await _tmux(f"new-session -d -s {session_name} '{cmd}'")
        await asyncio.sleep(2)
        output = await _tmux(f"capture-pane -t {session_name} -p -S -10")
        return f"会话 {session_name} 已创建。初始输出:\n{output}"
    except Exception as e:
        return f"创建失败: {e}"
