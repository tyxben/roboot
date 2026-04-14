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

import httpx
import yaml
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
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

LOCAL_SERVER = "https://localhost:8765"

# --- Arcana Runtime ---

_runtime: arcana.Runtime | None = None
_chat_sessions: dict[int, object] = {}  # telegram user_id → ChatSession

# Per-user state for interactive session management
# Maps user_id → {"session_id": str, "awaiting_command": bool}
_user_session_state: dict[int, dict] = {}


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


def _truncate_for_telegram(text: str, max_len: int = 4000) -> str:
    """Truncate text to fit within Telegram message limits."""
    if len(text) <= max_len:
        return text
    return text[:max_len] + "\n\n...(截断)"


# --- Helper: Build session content message ---

async def _build_session_content(session_id: str, project: str) -> tuple[str, InlineKeyboardMarkup]:
    """Read session content and build message + action keyboard."""
    from iterm_bridge import bridge

    content = await bridge.read_session(session_id, num_lines=50)

    # Build display text — use HTML mode to avoid Markdown parsing issues
    from html import escape
    header = f"📋 会话: {escape(project)}\n\n"
    escaped_content = escape(content or "(空)")

    full_text = header + f"<pre>{escaped_content}</pre>"
    # Telegram limit: 4096 chars
    if len(full_text) > 4000:
        available = 4000 - len(header) - 20
        escaped_content = "..." + escape(content[-(available):])
        full_text = header + f"<pre>{escaped_content}</pre>"

    # Action buttons
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🔄 刷新", callback_data=f"refresh:{session_id}"),
            InlineKeyboardButton("✏️ 发送命令", callback_data=f"cmd:{session_id}"),
        ],
        [
            InlineKeyboardButton("✅ 允许 (y)", callback_data=f"allow:{session_id}"),
            InlineKeyboardButton("❌ 拒绝 (n)", callback_data=f"deny:{session_id}"),
        ],
        [
            InlineKeyboardButton("⬅️ 返回列表", callback_data="back_to_list"),
        ],
    ])

    return full_text, keyboard


# --- Handlers ---


async def cmd_start(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("未授权。")
        return
    await update.message.reply_text(
        "你好，我是 Roboot。\n\n"
        "直接发消息跟我聊天，我可以帮你：\n"
        "- /sessions — 查看并管理 iTerm2 会话\n"
        "- /screenshot — 截屏查看桌面\n"
        "- /remote — 获取远程访问链接\n"
        "- /refresh — 刷新远程访问 token\n\n"
        "或直接发文字让 AI 帮你操作"
    )


async def cmd_sessions(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    from iterm_bridge import bridge

    sessions = await bridge.list_sessions()
    if not sessions:
        await update.message.reply_text("没有运行中的会话")
        return

    # Build inline keyboard with one button per session
    buttons = []
    for s in sessions:
        label = s.project or s.name or s.session_id[:8]
        buttons.append([InlineKeyboardButton(
            f"📂 {label}",
            callback_data=f"view:{s.session_id}",
        )])

    keyboard = InlineKeyboardMarkup(buttons)
    await update.message.reply_text(
        f"📋 {len(sessions)} 个会话 — 点击查看详情:",
        reply_markup=keyboard,
    )


async def callback_handler(update: Update, context) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    user_id = query.from_user.id
    print(f"[CALLBACK] user={user_id} data={query.data}", flush=True)

    if not _is_allowed(user_id):
        await query.answer("未授权")
        return

    await query.answer()  # Acknowledge the callback

    data = query.data
    from iterm_bridge import bridge

    # --- View session ---
    if data.startswith("view:"):
        session_id = data[5:]
        # Find the project name
        sessions = await bridge.list_sessions()
        project = session_id[:8]
        for s in sessions:
            if s.session_id == session_id:
                project = s.project or s.name
                break

        _user_session_state[user_id] = {
            "session_id": session_id,
            "project": project,
            "awaiting_command": False,
        }

        try:
            text, keyboard = await _build_session_content(session_id, project)
            try:
                await query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Edit failed: {e}")
                await query.message.reply_text(
                    text, reply_markup=keyboard, parse_mode="HTML",
                )
        except Exception as e:
            logger.error(f"View session error: {e}")
            await query.message.reply_text(f"读取会话失败: {e}")

    # --- Refresh session content ---
    elif data.startswith("refresh:"):
        session_id = data[8:]
        state = _user_session_state.get(user_id, {})
        project = state.get("project", session_id[:8])

        text, keyboard = await _build_session_content(session_id, project)
        try:
            await query.edit_message_text(
                text, reply_markup=keyboard, parse_mode="HTML",
            )
        except Exception:
            await query.message.reply_text(
                text, reply_markup=keyboard, parse_mode="HTML",
            )

    # --- Send command: prompt user to type ---
    elif data.startswith("cmd:"):
        session_id = data[4:]
        state = _user_session_state.get(user_id, {})
        _user_session_state[user_id] = {
            **state,
            "session_id": session_id,
            "awaiting_command": True,
        }
        project = state.get("project", session_id[:8])
        await query.edit_message_text(
            f"✏️ 请输入要发送到 [{project}] 的命令:\n\n"
            "(直接打字发送，下一条消息将作为命令发送到该会话)",
        )

    # --- Allow (send "y") ---
    elif data.startswith("allow:"):
        session_id = data[6:]
        result = await bridge.send_text(session_id, "y")
        state = _user_session_state.get(user_id, {})
        project = state.get("project", session_id[:8])

        if result == "sent":
            # Wait briefly for the session to process, then refresh
            await asyncio.sleep(1)
            text, keyboard = await _build_session_content(session_id, project)
            try:
                await query.edit_message_text(
                    f"✅ 已发送 'y' 到 {project}\n\n" + text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except Exception:
                await query.message.reply_text(
                    f"✅ 已发送 'y' 到 {project}",
                )
        else:
            await query.edit_message_text(f"发送失败: {result}")

    # --- Deny (send "n") ---
    elif data.startswith("deny:"):
        session_id = data[5:]
        result = await bridge.send_text(session_id, "n")
        state = _user_session_state.get(user_id, {})
        project = state.get("project", session_id[:8])

        if result == "sent":
            await asyncio.sleep(1)
            text, keyboard = await _build_session_content(session_id, project)
            try:
                await query.edit_message_text(
                    f"❌ 已发送 'n' 到 {project}\n\n" + text,
                    reply_markup=keyboard,
                    parse_mode="HTML",
                )
            except Exception:
                await query.message.reply_text(
                    f"❌ 已发送 'n' 到 {project}",
                )
        else:
            await query.edit_message_text(f"发送失败: {result}")

    # --- Back to session list ---
    elif data == "back_to_list":
        _user_session_state.pop(user_id, None)
        sessions = await bridge.list_sessions()
        if not sessions:
            await query.edit_message_text("没有运行中的会话")
            return

        buttons = []
        for s in sessions:
            label = s.project or s.name or s.session_id[:8]
            buttons.append([InlineKeyboardButton(
                f"📂 {label}",
                callback_data=f"view:{s.session_id}",
            )])

        keyboard = InlineKeyboardMarkup(buttons)
        await query.edit_message_text(
            f"📋 {len(sessions)} 个会话 — 点击查看详情:",
            reply_markup=keyboard,
        )


async def cmd_remote(update: Update, context) -> None:
    """Get remote access link from the local server's relay info."""
    if not _is_allowed(update.effective_user.id):
        return

    try:
        async with httpx.AsyncClient(verify=False) as client:
            resp = await client.get(f"{LOCAL_SERVER}/api/relay-info", timeout=5)
            data = resp.json()
    except Exception as e:
        await update.message.reply_text(
            f"无法连接本地服务器 ({LOCAL_SERVER})。\n\n"
            f"请确保 server.py 正在运行。\n错误: {e}"
        )
        return

    if data.get("enabled") and data.get("pairing_url"):
        url = data["pairing_url"]
        await update.message.reply_text(
            f"🌐 远程访问链接:\n\n{url}\n\n"
            "在浏览器中打开即可远程控制。\n"
            "使用 /refresh 可刷新 token。"
        )
    else:
        await update.message.reply_text(
            "远程中继未启用。\n\n"
            "请在 config.yaml 中配置 remote_access 并重启 server.py:\n\n"
            "```yaml\nremote_access:\n  method: official_relay\n  relay:\n    enabled: true\n```",
            parse_mode="HTML",
        )


async def cmd_refresh(update: Update, context) -> None:
    """Refresh relay token and return new URL."""
    if not _is_allowed(update.effective_user.id):
        return

    try:
        async with httpx.AsyncClient(verify=False) as client:
            # Try the refresh endpoint first
            try:
                resp = await client.post(
                    f"{LOCAL_SERVER}/api/relay-refresh", timeout=5,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    url = data.get("pairing_url", "")
                    if url:
                        await update.message.reply_text(
                            f"🔄 Token 已刷新!\n\n🌐 新链接:\n{url}"
                        )
                        return
            except Exception:
                pass

            # Fallback: return current relay info
            resp = await client.get(f"{LOCAL_SERVER}/api/relay-info", timeout=5)
            data = resp.json()

            if data.get("enabled") and data.get("pairing_url"):
                url = data["pairing_url"]
                await update.message.reply_text(
                    f"刷新接口暂不可用，返回当前链接:\n\n🌐 {url}"
                )
            else:
                await update.message.reply_text("远程中继未启用。请查看 /remote 获取配置说明。")

    except Exception as e:
        await update.message.reply_text(
            f"无法连接本地服务器。请确保 server.py 正在运行。\n错误: {e}"
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
    """Handle text messages — route through Arcana agent or send to session."""
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    text = update.message.text
    if not text:
        return

    # Check if user is in "awaiting command" mode for a session
    state = _user_session_state.get(user_id, {})
    print(f"[MSG] user={user_id} text='{text[:30]}' awaiting={state.get('awaiting_command')}", flush=True)
    if state.get("awaiting_command"):
        session_id = state["session_id"]
        project = state.get("project", session_id[:8])
        _user_session_state[user_id]["awaiting_command"] = False
        logger.info(f"Sending '{text}' to session {project} ({session_id})")

        try:
            from iterm_bridge import bridge
            result = await bridge.send_text(session_id, text)
            logger.info(f"send_text result: {result}")
        except Exception as e:
            logger.error(f"send_text error: {e}")
            await update.message.reply_text(f"发送失败: {e}")
            return

        if result == "sent":
            await asyncio.sleep(1)
            try:
                content_text, keyboard = await _build_session_content(session_id, project)
                from html import escape
                reply = f"✅ 已发送到 {escape(project)}:\n<code>{escape(text)}</code>\n\n" + content_text
                if len(reply) > 4000:
                    reply = reply[:4000]
                await update.message.reply_text(
                    reply, reply_markup=keyboard, parse_mode="HTML",
                )
            except Exception as e:
                logger.error(f"Send command display error: {e}")
                await update.message.reply_text(f"✅ 已发送到 {project}")
        else:
            await update.message.reply_text(f"发送失败: {result}")
        return

    # Normal chat flow — route through Arcana agent
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
    app.add_handler(CommandHandler("remote", cmd_remote))
    app.add_handler(CommandHandler("refresh", cmd_refresh))
    app.add_handler(CallbackQueryHandler(callback_handler))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    print("Roboot Telegram Bot 已启动！")
    app.run_polling()


if __name__ == "__main__":
    main()
