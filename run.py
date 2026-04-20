#!/usr/bin/env python3
"""Roboot — personal AI agent hub.

Usage:
    python run.py              # 键盘模式
    python run.py --voice      # 语音模式
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

import yaml

import arcana

# --- Tools ---
from tools.shell import shell
from tools.claude_code import (
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
)
from tools.vision import look, screenshot
from tools.soul import build_personality, summarize_sessions

ALL_TOOLS = [
    shell,
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
    look,
    screenshot,
]


def load_config() -> dict:
    """Load config.yaml, fall back to env vars."""
    config_path = os.path.join(os.path.dirname(__file__), "config.yaml")
    if os.path.exists(config_path):
        with open(config_path) as f:
            return yaml.safe_load(f) or {}

    # Fallback: minimal config from env vars
    return {
        "providers": {
            "deepseek": os.environ.get("DEEPSEEK_API_KEY", ""),
            "anthropic": os.environ.get("ANTHROPIC_API_KEY", ""),
            "zhipu": os.environ.get("ZHIPU_API_KEY", ""),
        },
        "default_provider": "deepseek",
        "personality": "你是 Roboot，一个友好简洁的 AI 助手。",
    }


def build_io(config: dict, voice_mode: bool):
    """Create the I/O adapter."""
    if voice_mode:
        from adapters.voice import VoiceIO

        voice_cfg = config.get("voice", {})
        return VoiceIO(
            language=voice_cfg.get("language", "zh-CN"),
            tts_voice=voice_cfg.get("tts_voice", ""),
        )
    else:
        from adapters.keyboard import KeyboardIO

        return KeyboardIO()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Roboot AI Agent")
    parser.add_argument("--voice", action="store_true", help="启用语音模式")
    args = parser.parse_args()

    config = load_config()

    # Filter out empty API keys
    providers = {k: v for k, v in config.get("providers", {}).items() if v and not v.startswith("sk-...")}
    if not providers:
        print("请先配置 API key:")
        print("  1. cp config.example.yaml config.yaml")
        print("  2. 填入至少一个 provider 的 API key")
        print("  或设置环境变量: DEEPSEEK_API_KEY / ANTHROPIC_API_KEY / ZHIPU_API_KEY")
        sys.exit(1)

    default_provider = config.get("default_provider", next(iter(providers)))
    budget_usd = config.get("daily_budget_usd", 5.0)
    # Build the full dynamic personality (soul.md + current-context block).
    # Channel depends on whether --voice was passed.
    channel = "voice" if args.voice else "cli"
    sessions_summary = await summarize_sessions()
    personality = build_personality(
        channel=channel, sessions_summary=sessions_summary
    )

    # Build Arcana Runtime
    runtime = arcana.Runtime(
        providers=providers,
        tools=ALL_TOOLS,
        budget=arcana.Budget(max_cost_usd=budget_usd),
        config=arcana.RuntimeConfig(
            default_provider=default_provider,
            default_model=config.get("default_model"),
        ),
    )

    # Build I/O
    io_adapter = build_io(config, args.voice)

    print("Roboot 已启动 (Ctrl+C 退出)")
    print(f"模型: {default_provider} | 预算: ${budget_usd}")
    if args.voice:
        print("模式: 语音")
    else:
        print("模式: 键盘 (加 --voice 启用语音)\n")

    try:
        async with runtime.chat(system_prompt=personality) as session:
            await io_adapter.speak("你好，我是 Roboot。")

            while True:
                text = await io_adapter.listen()
                if text is None:
                    continue
                if text.lower() in ("exit", "quit", "再见", "退出"):
                    await io_adapter.speak("再见！")
                    break

                response = await session.send(text)
                await io_adapter.speak(response.content)

            print(f"\n会话统计: {session.total_tokens} tokens, ${session.total_cost_usd:.4f}")
    except KeyboardInterrupt:
        print("\n再见！")
    finally:
        await runtime.close()


if __name__ == "__main__":
    asyncio.run(main())
