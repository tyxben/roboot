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
import re
from html import escape
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

import chat_store
import memory
from tools.shell import shell
from tools.claude_code import (
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
)
from tools.vision import screenshot
from tools.soul import build_personality, summarize_sessions

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
# telegram user_id → chat_store session_id for transcript + replay + distill.
_history_ids: dict[int, str] = {}

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


async def _build_telegram_personality() -> str:
    """Full personality for Telegram sessions, with current-context block."""
    summary = await summarize_sessions()
    return build_personality(channel="telegram", sessions_summary=summary)


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


# ANSI CSI / OSC sequences + stray control chars can break Telegram HTML parsing.
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07?|\x1b[@-_]")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _sanitize_terminal_text(text: str) -> str:
    """Strip ANSI escapes and stray control characters from terminal output.

    Telegram rejects messages containing bell (\\x07), NUL, and other low-ASCII
    control chars even inside <pre> blocks, which silently fails the HTML parse.
    """
    if not text:
        return ""
    text = _ANSI_RE.sub("", text)
    text = _CTRL_RE.sub("", text)
    return text


async def _safe_reply(target, text: str, reply_markup=None, *, edit: bool = False) -> bool:
    """Send a message, preferring HTML; fall back cleanly to plain text on parse failure.

    `target` should be either a `CallbackQuery` (when edit=True) or a `Message`.
    Returns True on success.
    """
    kwargs = {"reply_markup": reply_markup} if reply_markup else {}
    try:
        if edit:
            await target.edit_message_text(text, parse_mode="HTML", **kwargs)
        else:
            await target.reply_text(text, parse_mode="HTML", **kwargs)
        return True
    except Exception as e:
        logger.warning("HTML send failed (%s); falling back to plain text", e)

    # Plain text fallback — strip HTML tags, unescape entities.
    plain = re.sub(r"<[^>]+>", "", text)
    plain = (plain.replace("&lt;", "<").replace("&gt;", ">")
                    .replace("&amp;", "&").replace("&quot;", '"').replace("&#x27;", "'"))
    try:
        if edit:
            await target.edit_message_text(plain[:4000], **kwargs)
        else:
            await target.reply_text(plain[:4000], **kwargs)
        return True
    except Exception as e:
        logger.error("Plain text fallback also failed: %s", e)
        return False


# --- Helper: Build session content message ---

async def _build_session_content(session_id: str, project: str) -> tuple[str, InlineKeyboardMarkup]:
    """Read session content and build message + action keyboard."""
    from iterm_bridge import bridge

    raw = await bridge.read_session(session_id, num_lines=50)
    content = _sanitize_terminal_text(raw) or "(空)"

    # Build display text — use HTML mode to avoid Markdown parsing issues.
    header = f"📋 会话: {escape(project)}\n\n"
    escaped_content = escape(content)

    # Telegram limit: 4096 chars. Reserve headroom for header + <pre></pre>.
    max_body = 4000 - len(header) - len("<pre></pre>") - 20
    if len(escaped_content) > max_body:
        # Keep the tail (most recent terminal output) and re-escape from a
        # safe slice of the sanitized (not yet escaped) content so we never
        # slice mid-entity.
        tail = content[-max_body:]
        escaped_content = "..." + escape(tail)

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


async def _render_session_view(query, session_id: str, project: str, prefix_html: str = "") -> None:
    """Render (or re-render) a session's content view with action buttons.

    Tries to edit the current message; if that fails (HTML parse, message
    deleted, etc.), posts a fresh reply. `prefix_html` is prepended to the body
    and must be already HTML-safe (escape any variable text).
    """
    try:
        body, keyboard = await _build_session_content(session_id, project)
    except Exception as e:
        logger.exception("read_session failed for %s", session_id)
        await _safe_reply(query.message, f"读取会话失败: {escape(str(e))}")
        return

    text = (prefix_html + body) if prefix_html else body
    if len(text) > 4000:
        # Truncate the prefix, never the body (body already fits and has
        # balanced <pre> tags).
        if len(body) <= 4000:
            text = body
        else:
            text = text[:4000]

    if not await _safe_reply(query, text, reply_markup=keyboard, edit=True):
        await _safe_reply(query.message, text, reply_markup=keyboard)


async def callback_handler(update: Update, context) -> None:
    """Handle inline keyboard button presses."""
    query = update.callback_query
    user_id = query.from_user.id
    logger.info("[CALLBACK] user=%s data=%s", user_id, query.data)

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
        try:
            sessions = await bridge.list_sessions()
        except Exception as e:
            logger.exception("list_sessions failed in view callback")
            await _safe_reply(query.message, f"无法连接 iTerm2: {escape(str(e))}")
            return

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
        await _render_session_view(query, session_id, project)

    # --- Refresh session content ---
    elif data.startswith("refresh:"):
        session_id = data[8:]
        state = _user_session_state.get(user_id, {})
        project = state.get("project", session_id[:8])
        await _render_session_view(query, session_id, project)

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
        # Plain text — no parse_mode needed, project may contain special chars.
        try:
            await query.edit_message_text(
                f"✏️ 请输入要发送到 [{project}] 的命令:\n\n"
                "(直接打字发送，下一条消息将作为命令发送到该会话)",
            )
        except Exception as e:
            logger.warning("edit for cmd prompt failed: %s", e)
            await query.message.reply_text(
                f"✏️ 请输入要发送到 [{project}] 的命令:\n\n"
                "(直接打字发送，下一条消息将作为命令发送到该会话)",
            )

    # --- Allow (send "y") ---
    elif data.startswith("allow:"):
        session_id = data[6:]
        try:
            result = await bridge.send_text(session_id, "y")
        except Exception as e:
            logger.exception("send_text y failed")
            await _safe_reply(query.message, f"发送失败: {escape(str(e))}")
            return
        state = _user_session_state.get(user_id, {})
        project = state.get("project", session_id[:8])

        if result == "sent":
            await asyncio.sleep(1)
            prefix = f"✅ 已发送 'y' 到 {escape(project)}\n\n"
            await _render_session_view(query, session_id, project, prefix_html=prefix)
        else:
            await _safe_reply(query, f"发送失败: {escape(str(result))}", edit=True)

    # --- Deny (send "n") ---
    elif data.startswith("deny:"):
        session_id = data[5:]
        try:
            result = await bridge.send_text(session_id, "n")
        except Exception as e:
            logger.exception("send_text n failed")
            await _safe_reply(query.message, f"发送失败: {escape(str(e))}")
            return
        state = _user_session_state.get(user_id, {})
        project = state.get("project", session_id[:8])

        if result == "sent":
            await asyncio.sleep(1)
            prefix = f"❌ 已发送 'n' 到 {escape(project)}\n\n"
            await _render_session_view(query, session_id, project, prefix_html=prefix)
        else:
            await _safe_reply(query, f"发送失败: {escape(str(result))}", edit=True)

    # --- Back to session list ---
    elif data == "back_to_list":
        _user_session_state.pop(user_id, None)
        try:
            sessions = await bridge.list_sessions()
        except Exception as e:
            logger.exception("list_sessions failed in back_to_list")
            await _safe_reply(query.message, f"无法连接 iTerm2: {escape(str(e))}")
            return

        if not sessions:
            try:
                await query.edit_message_text("没有运行中的会话")
            except Exception:
                await query.message.reply_text("没有运行中的会话")
            return

        buttons = []
        for s in sessions:
            label = s.project or s.name or s.session_id[:8]
            buttons.append([InlineKeyboardButton(
                f"📂 {label}",
                callback_data=f"view:{s.session_id}",
            )])

        keyboard = InlineKeyboardMarkup(buttons)
        try:
            await query.edit_message_text(
                f"📋 {len(sessions)} 个会话 — 点击查看详情:",
                reply_markup=keyboard,
            )
        except Exception:
            await query.message.reply_text(
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
    logger.info("[MSG] user=%s text=%r awaiting=%s", user_id, text[:60], state.get("awaiting_command"))
    if state.get("awaiting_command"):
        session_id = state["session_id"]
        project = state.get("project", session_id[:8])
        _user_session_state[user_id]["awaiting_command"] = False
        logger.info("Sending %r to session %s (%s)", text, project, session_id)

        try:
            from iterm_bridge import bridge
            result = await bridge.send_text(session_id, text)
            logger.info("send_text result: %s", result)
        except Exception as e:
            logger.exception("send_text error")
            await update.message.reply_text(f"发送失败: {e}")
            return

        if result != "sent":
            await update.message.reply_text(f"发送失败: {result}")
            return

        # Brief pause so the terminal can process the command before we read it.
        await asyncio.sleep(1)

        # Send a short confirmation first (always delivers, even if rendering
        # the terminal view fails).
        ack = f"✅ 已发送到 <b>{escape(project)}</b>:\n<code>{escape(text)}</code>"
        await _safe_reply(update.message, ack)

        # Then send the refreshed session view as a separate message. This
        # avoids any risk of the combined message exceeding 4096 chars and
        # getting sliced mid-HTML-tag.
        try:
            content_text, keyboard = await _build_session_content(session_id, project)
            await _safe_reply(update.message, content_text, reply_markup=keyboard)
        except Exception as e:
            logger.exception("build_session_content after send failed")
            await update.message.reply_text(f"(无法刷新会话视图: {e})")
        return

    # Normal chat flow — route through Arcana agent
    runtime = _get_runtime()
    if user_id not in _chat_sessions:
        personality = await _build_telegram_personality()
        _chat_sessions[user_id] = runtime.create_chat_session(
            system_prompt=personality
        )
        # Layer A: first time we see this user in this process -- if there's
        # a prior telegram history row we can't know its id, so we create a
        # fresh one here. If callers persist user->session_id mapping
        # (e.g. via a tiny shelve/json), they can set _history_ids[user_id]
        # before the first message and replay_history will fire then.
        _history_ids[user_id] = await chat_store.create_session(
            source="telegram", label=str(user_id)
        )

    session = _chat_sessions[user_id]
    history_id = _history_ids.get(user_id)

    # Show typing
    await update.message.chat.send_action("typing")

    try:
        if history_id:
            await chat_store.record_user(history_id, text)
        response = await session.send(text)
        reply = response.content or "(无回复)"
        if history_id:
            await chat_store.record_assistant(history_id, reply, 0)
        # Telegram message limit is 4096 chars
        if len(reply) > 4000:
            reply = reply[:4000] + "\n\n...(截断)"
        await update.message.reply_text(reply)
        # Layer B: count turns; fire distillation every K.
        memory.record_turn_and_maybe_distill(history_id, runtime=runtime)
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


async def _notify_allowed_users(tg_app, payload: dict) -> None:
    """Session-watcher subscriber: DM every allowed user when a Claude Code
    session enters a waiting-for-confirmation state.

    Individual send failures (network, user never started the bot, etc.)
    are swallowed so the watcher keeps firing for other subscribers.
    """
    allowed = CONFIG.get("telegram", {}).get("allowed_users", []) or []
    if not allowed:
        return
    project = payload.get("project", "?")
    prompt_line = payload.get("prompt_line", "")
    text = f"🔔 Session {project}: {prompt_line}"
    for uid in allowed:
        try:
            await tg_app.bot.send_message(chat_id=uid, text=text[:4000])
        except Exception as e:  # pragma: no cover - network dependent
            logger.warning("notify %s failed: %s", uid, e)


def _register_session_watcher(tg_app) -> None:
    """Start the session watcher inside the bot's event loop.

    The telegram `Application` exposes `post_init`, which runs after the
    bot's loop is up. Registering the subscriber there guarantees
    `asyncio.create_task()` inside watcher.start() grabs the right loop.
    """
    from session_watcher import watcher

    async def _post_init(_app):
        async def _cb(payload):
            await _notify_allowed_users(tg_app, payload)

        watcher.subscribe(_cb)
        watcher.start()

    tg_app.post_init = _post_init


def main():
    # Surface our own logs (and WARN+ from libs) so debugging the bot locally
    # — especially the session view / send-command flows — is painless.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)

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

    _register_session_watcher(app)

    print("Roboot Telegram Bot 已启动！")
    app.run_polling()


if __name__ == "__main__":
    main()
