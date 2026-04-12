"""Roboot — Chainlit frontend with Arcana agent backend.

Usage:
    chainlit run chainlit_app.py -w
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

import arcana
import chainlit as cl

from tools.shell import shell
from tools.claude_code import (
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
)
from tools.vision import look, screenshot

ALL_TOOLS = [
    shell,
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
    look,
    screenshot,
]

# --- Config ---


def _load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        return yaml.safe_load(config_path.read_text()) or {}
    return {}


CONFIG = _load_config()
PROVIDERS = {
    k: v
    for k, v in CONFIG.get("providers", {}).items()
    if v and not v.startswith("sk-...")
}
PERSONALITY = CONFIG.get("personality", "你是 Roboot，一个简洁友好的 AI 助手。")

# --- Arcana Runtime (shared across sessions) ---

_runtime: arcana.Runtime | None = None


def get_runtime() -> arcana.Runtime:
    global _runtime
    if _runtime is None:
        _runtime = arcana.Runtime(
            providers=PROVIDERS,
            tools=ALL_TOOLS,
            budget=arcana.Budget(max_cost_usd=CONFIG.get("daily_budget_usd", 5.0)),
            config=arcana.RuntimeConfig(
                default_provider=CONFIG.get("default_provider", "deepseek"),
                default_model=CONFIG.get("default_model"),
            ),
        )
    return _runtime


# --- Chainlit Hooks ---


@cl.on_chat_start
async def on_chat_start():
    """Create a new Arcana chat session for this user."""
    runtime = get_runtime()
    session = runtime.create_chat_session(system_prompt=PERSONALITY)
    cl.user_session.set("session", session)

    await cl.Message(
        content="你好，我是 **Roboot** — 你的私人 AI 助手。\n\n"
        "我可以帮你执行终端命令、管理 Claude Code 会话、拍照、截屏等。\n"
        "试试说：`帮我看看当前目录有什么文件`",
    ).send()


@cl.on_message
async def on_message(msg: cl.Message):
    """Handle user message → Arcana agent → response with tool steps."""
    session = cl.user_session.get("session")
    if session is None:
        await cl.Message(content="会话已断开，请刷新页面。").send()
        return

    response = await session.send(msg.content)

    # Show tool calls as Steps
    if response.tool_calls_made and response.tool_calls_made > 0:
        async with cl.Step(
            name="工具调用",
            type="tool",
            show_input=False,
        ) as step:
            step.output = f"执行了 {response.tool_calls_made} 个工具调用"

    # Send response
    reply = cl.Message(
        content=response.content or "(无回复)",
        metadata={
            "tokens": response.tokens_used,
            "cost_usd": f"${response.cost_usd:.4f}",
            "tools": response.tool_calls_made,
        },
    )
    await reply.send()


@cl.set_starters
async def starters():
    """Quick-start buttons shown on empty chat."""
    return [
        cl.Starter(
            label="查看文件",
            message="帮我看看当前目录有什么文件",
            icon="/public/folder.svg",
        ),
        cl.Starter(
            label="Claude Code 状态",
            message="有没有 tmux 会话在跑",
            icon="/public/terminal.svg",
        ),
        cl.Starter(
            label="系统信息",
            message="告诉我现在的时间，以及这台电脑的 macOS 版本",
            icon="/public/info.svg",
        ),
        cl.Starter(
            label="截屏",
            message="截个屏让我看看",
            icon="/public/camera.svg",
        ),
    ]
