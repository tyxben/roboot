"""Shell command execution tool."""

from __future__ import annotations

import asyncio
import subprocess

import arcana

DANGEROUS_PATTERNS = [
    "rm -rf /",
    "rm -rf ~",
    "mkfs",
    "dd if=",
    "> /dev/",
    "shutdown",
    "reboot",
    ":(){ :|:& };:",
]


@arcana.tool(
    when_to_use="当用户让你在电脑上做任何事情：查文件、开应用、跑命令、查系统信息、操作 git 等",
    what_to_expect="命令的 stdout/stderr 输出，最多 4000 字符",
    failure_meaning="命令执行失败或超时，检查命令是否正确",
    side_effect="read",
    requires_confirmation=False,
)
async def shell(command: str) -> str:
    """在用户的 Mac 终端执行 shell 命令。"""
    for pattern in DANGEROUS_PATTERNS:
        if pattern in command:
            return f"拒绝执行危险命令: {command}"

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=30)
        output = (stdout or b"").decode() + (stderr or b"").decode()
        return output[:4000] or "(无输出)"
    except asyncio.TimeoutError:
        return "命令执行超时（30秒）"
    except Exception as e:
        return f"执行失败: {e}"
