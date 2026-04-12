"""Roboot Telegram Bot — remote control your Mac from anywhere.

Usage:
    python -m adapters.telegram_bot

Setup:
    1. Talk to @BotFather on Telegram, create a bot, get the token
    2. Add TELEGRAM_BOT_TOKEN to config.yaml or .env
    3. Optional: set telegram.allowed_users to restrict access
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

import yaml
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
)

import arcana

from tools.shell import shell
from tools.claude_code import (
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
)
from tools.vision import screenshot

logger = logging.getLogger(__name__)

ALL_TOOLS = [
    shell,
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
    screenshot,
]


def _load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config.yaml"
    if config_path.exists():
        return yaml.safe_load(config_path.read_text()) or {}
    return {}


CONFIG = _load_config()

# --- Arcana Runtime ---

_runtime: arcana.Runtime | None = None
_chat_sessions: dict[int, object] = {}  # telegram user_id → ChatSession


def _get_runtime() -> arcana.Runtime:
    global _runtime
    if _runtime is None:
        providers = {
            k: v
            for k, v in CONFIG.get("providers", {}).items()
            if v and not v.startswith("sk-...")
        }
        _runtime = arcana.Runtime(
            providers=providers,
            tools=ALL_TOOLS,
            budget=arcana.Budget(max_cost_usd=CONFIG.get("daily_budget_usd", 5.0)),
            config=arcana.RuntimeConfig(
                default_provider=CONFIG.get("default_provider", "deepseek"),
                default_model=CONFIG.get("default_model"),
            ),
        )
    return _runtime


def _get_personality() -> str:
    base = CONFIG.get("personality", "你是 Roboot，一个简洁友好的 AI 助手。")
    return base + "\n\n你现在通过 Telegram 和用户远程交流。用户不在电脑前，可能在外面。回答要更简洁。"


def _is_allowed(user_id: int) -> bool:
    """Check if this Telegram user is allowed to use the bot."""
    allowed = CONFIG.get("telegram", {}).get("allowed_users", [])
    if not allowed:
        return True  # No restriction if not configured
    return user_id in allowed


# --- Handlers ---


async def cmd_start(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("未授权。")
        return
    await update.message.reply_text(
        "你好，我是 Roboot。\n\n"
        "直接发消息跟我聊天，我可以帮你：\n"
        "- 查看 Claude Code 会话状态\n"
        "- 让某个会话执行操作\n"
        "- 在 Mac 上跑命令\n"
        "- 截屏看桌面\n\n"
        "试试：有哪些会话在跑"
    )


async def cmd_sessions(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    from iterm_bridge import bridge

    sessions = await bridge.list_sessions()
    if not sessions:
        await update.message.reply_text("没有运行中的会话")
        return
    lines = [f"• **{s.project}** — {s.name}" for s in sessions]
    await update.message.reply_text(
        f"📋 {len(sessions)} 个会话:\n\n" + "\n".join(lines),
        parse_mode="Markdown",
    )


async def cmd_screenshot(update: Update, context) -> None:
    """Take a screenshot and send it."""
    if not _is_allowed(update.effective_user.id):
        return
    await update.message.reply_text("截屏中...")
    from tools.vision import _capture_screenshot

    img = _capture_screenshot()
    if img:
        await update.message.reply_photo(photo=img, caption="当前桌面")
    else:
        await update.message.reply_text("截屏失败")


async def handle_message(update: Update, context) -> None:
    """Handle text messages — route through Arcana agent."""
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    text = update.message.text
    if not text:
        return

    # Get or create chat session for this user
    if user_id not in _chat_sessions:
        runtime = _get_runtime()
        _chat_sessions[user_id] = runtime.create_chat_session(
            system_prompt=_get_personality()
        )

    session = _chat_sessions[user_id]

    # Show typing
    await update.message.chat.send_action("typing")

    try:
        response = await session.send(text)
        reply = response.content or "(无回复)"
        # Telegram message limit is 4096 chars
        if len(reply) > 4000:
            reply = reply[:4000] + "\n\n...(截断)"
        await update.message.reply_text(reply)
    except Exception as e:
        logger.error(f"Agent error: {e}")
        await update.message.reply_text(f"出错了: {e}")


async def handle_voice(update: Update, context) -> None:
    """Handle voice messages — download, transcribe, then process as text."""
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    await update.message.chat.send_action("typing")

    # Download voice file
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    file = await voice.get_file()
    ogg_path = f"/tmp/roboot_voice_{user_id}.ogg"
    wav_path = f"/tmp/roboot_voice_{user_id}.wav"
    await file.download_to_drive(ogg_path)

    # Convert to wav and transcribe
    try:
        proc = await asyncio.create_subprocess_shell(
            f"ffmpeg -y -i {ogg_path} -ar 16000 -ac 1 {wav_path}",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        await proc.communicate()

        # Try whisper for transcription
        try:
            import speech_recognition as sr

            recognizer = sr.Recognizer()
            with sr.AudioFile(wav_path) as source:
                audio = recognizer.record(source)
            text = recognizer.recognize_google(audio, language="zh-CN")
        except ImportError:
            # Fallback: use Google's API via a simpler method
            await update.message.reply_text("语音识别需要安装 SpeechRecognition: pip install SpeechRecognition")
            return

        if not text:
            await update.message.reply_text("没听清，再说一次？")
            return

        await update.message.reply_text(f"🎤 识别: {text}")

        # Process as text message
        update.message.text = text
        await handle_message(update, context)

    except Exception as e:
        logger.error(f"Voice error: {e}")
        await update.message.reply_text(f"语音处理失败: {e}")
    finally:
        for p in [ogg_path, wav_path]:
            try:
                os.unlink(p)
            except OSError:
                pass


def main():
    token = CONFIG.get("telegram", {}).get("bot_token", "") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("需要 Telegram Bot Token:")
        print("  1. 在 Telegram 找 @BotFather 创建 Bot")
        print("  2. 在 config.yaml 添加:")
        print("     telegram:")
        print("       bot_token: 'your-token'")
        print("  或设置环境变量 TELEGRAM_BOT_TOKEN")
        return

    print("Roboot Telegram Bot 启动中...")

    app = Application.builder().token(token).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("screenshot", cmd_screenshot))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Roboot Telegram Bot 已启动！")
    app.run_polling()


if __name__ == "__main__":
    main()
