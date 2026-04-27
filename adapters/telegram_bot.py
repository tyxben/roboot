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
import tool_guard
from text_utils import extract_spoken_text
from adapters.stt import get_backend as _get_stt_backend
from adapters.tts_streamer import (
    DEFAULT_VOICE as TTS_DEFAULT_VOICE,
    segment_for_tts,
    synthesize_ogg,
    synthesize_segments_parallel,
)
from adapters import voice_prefs


# Curated voice picker menu. Keep this short — Telegram keyboards get
# cluttered past ~10 buttons. Each entry is (voice_name, display_label).
VOICE_CHOICES: list[tuple[str, str]] = [
    ("zh-CN-YunxiNeural", "云希 · 男声沉稳"),
    ("zh-CN-YunjianNeural", "云健 · 男声深沉"),
    ("zh-CN-YunyangNeural", "云扬 · 男声新闻"),
    ("zh-CN-YunxiaNeural", "云夏 · 男童声"),
    ("zh-CN-XiaoxiaoNeural", "晓晓 · 女声温暖"),
    ("zh-CN-XiaoyiNeural", "晓伊 · 女声活泼"),
    ("zh-CN-liaoning-XiaobeiNeural", "晓贝 · 东北话"),
    ("zh-CN-shaanxi-XiaoniNeural", "晓妮 · 陕西话"),
    ("en-US-JennyNeural", "Jenny · EN female"),
    ("en-US-GuyNeural", "Guy · EN male"),
]
_VALID_VOICES: set[str] = {v for v, _ in VOICE_CHOICES}


def _resolve_tts_voice(user_id: int | None = None) -> str:
    """Pick the voice for a reply. Per-user override (`/voice` picker) wins
    over config.yaml's `voice.tts_voice`, which wins over the tts_streamer
    default."""
    if user_id is not None:
        pref = voice_prefs.get_voice(user_id)
        if pref:
            return pref
    v = (CONFIG.get("voice") or {}).get("tts_voice") or ""
    v = v.strip()
    return v or TTS_DEFAULT_VOICE
from tools.shell import shell
from tools.claude_code import (
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
)
from tools.vision import screenshot
from tools.soul import build_personality, summarize_sessions
from tools.voice_switch import current_tg_user, switch_tts_voice

logger = logging.getLogger(__name__)

ALL_TOOLS = [
    shell,
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
    screenshot,
    switch_tts_voice,
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
        # Wire the approval gate. Same private-attribute poke as server.py:
        # Arcana 0.8.x has no public setter. Without this, Telegram-driven
        # `run_command` calls bypass the gate entirely (D2 only wired the
        # daemon's runtime).
        if _runtime._tool_gateway is not None:
            _runtime._tool_gateway.confirmation_callback = (
                tool_guard.confirmation_callback
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


_HELP_TEXT = (
    "📋 <b>Roboot 命令</b>\n\n"
    "- /sessions — 查看并管理 iTerm2 会话\n"
    "- /screenshot — 截屏查看桌面\n"
    "- /voice — 切换 AI 朗读时用的声音\n"
    "- /remote — 获取远程访问链接\n"
    "- /refresh — 刷新远程访问 token\n"
    "- /help — 显示这份命令清单\n\n"
    "不想记命令也行 —— 直接发文字或语音，"
    "比如\"帮我截个屏\"、\"换成女声\"、\"看看正在跑的 session\""
    "，AI 会自动调用对应工具。"
)


async def cmd_start(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        await update.message.reply_text("未授权。")
        return
    await _safe_reply(
        update.message,
        "你好，我是 Roboot。\n\n" + _HELP_TEXT,
    )


async def cmd_help(update: Update, context) -> None:
    if not _is_allowed(update.effective_user.id):
        return
    await _safe_reply(update.message, _HELP_TEXT)


async def cmd_voice(update: Update, context) -> None:
    """`/voice` picker — show current voice and let the user switch.

    Accepts optional inline arg (`/voice zh-CN-XiaoxiaoNeural`) for power
    users who know exactly what they want. No arg → inline keyboard."""
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    args = (context.args or []) if hasattr(context, "args") else []
    if args:
        requested = args[0].strip()
        if requested not in _VALID_VOICES and not _looks_like_edge_voice(requested):
            await update.message.reply_text(
                f"不认识这个声音名: {requested}\n"
                "用 /voice 打开选择菜单，或给个完整的 edge-tts 名(如 zh-CN-XiaoxiaoNeural)。"
            )
            return
        voice_prefs.set_voice(user_id, requested)
        await _send_voice_sample(update, requested, label=requested)
        return

    current = _resolve_tts_voice(user_id)
    buttons = []
    row: list[InlineKeyboardButton] = []
    for name, label in VOICE_CHOICES:
        marker = "✓ " if name == current else ""
        row.append(InlineKeyboardButton(
            f"{marker}{label}",
            callback_data=f"voice:{name}",
        ))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    await update.message.reply_text(
        f"当前声音: <code>{escape(current)}</code>\n选一个切换:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="HTML",
    )


def _looks_like_edge_voice(name: str) -> bool:
    """Cheap sanity check — `xx-XX-<Something>Neural`. We don't hit the
    edge-tts voice list endpoint on every command to avoid extra latency."""
    return bool(name) and name.endswith("Neural") and "-" in name


async def _send_voice_sample(target, voice: str, label: str) -> None:
    """After switching, synthesize a short sample in the new voice so the
    user hears the change immediately. `target` can be an `Update` (when
    called from `/voice <name>`) or a `CallbackQuery` (when called from an
    inline-button pick) — both expose `.message.reply_voice/reply_text`.
    Falls back to a text confirmation if synthesis fails (e.g. edge-tts blip)."""
    message = getattr(target, "message", None) or target
    sample_text = f"你好，我现在是 {label}。"
    try:
        ogg = await synthesize_ogg(sample_text, voice=voice)
    except Exception as e:
        logger.warning("voice sample synth failed: %s", e)
        await message.reply_text(f"已切换到 {voice}，但试听合成失败: {e}")
        return
    try:
        await message.reply_voice(voice=ogg, caption=f"✓ 已切换为 {label}")
    except Exception as e:
        logger.warning("voice sample send failed: %s", e)
        await message.reply_text(f"已切换为 {voice}(试听发送失败: {e})")


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

    # --- Voice picker selection ---
    elif data.startswith("voice:"):
        voice = data[len("voice:"):]
        if voice not in _VALID_VOICES:
            await _safe_reply(query, f"未知声音: {escape(voice)}", edit=True)
            return
        voice_prefs.set_voice(user_id, voice)
        label = next((l for v, l in VOICE_CHOICES if v == voice), voice)
        try:
            await query.edit_message_text(
                f"✓ 已切换为 <b>{escape(label)}</b>\n<code>{escape(voice)}</code>",
                parse_mode="HTML",
            )
        except Exception:
            pass
        await _send_voice_sample(query, voice, label=label)

    # --- Tool approval (allow / deny) ---
    elif data.startswith("tool_ok:") or data.startswith("tool_no:"):
        approved = data.startswith("tool_ok:")
        req_id = data.split(":", 1)[1]
        owner = _pending_owner.get(req_id)
        # Refuse cross-user clicks: only the user the modal was sent to may
        # answer. Without this, any allowed user who learned a req_id could
        # approve another user's tool. The targeted DM doesn't enforce it
        # by itself — callback_data is just bytes back to the bot.
        if owner is not None and owner != user_id:
            await query.answer("这不是你的批准请求", show_alert=True)
            return
        resolved = tool_guard.resolve_decision(req_id, approved)
        _pending_owner.pop(req_id, None)
        if not resolved:
            # Future already gone — user clicked after the 30s timeout, or
            # the daemon raced ahead and rejected. Tell them, don't pretend
            # the click did anything.
            try:
                await query.edit_message_text("⏰ 该工具批准请求已超时或已处理。")
            except Exception:
                pass
            return
        verdict = "✅ 已批准" if approved else "❌ 已拒绝"
        try:
            await query.edit_message_text(verdict)
        except Exception:
            pass


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


async def _agent_reply(user_id: int, text: str) -> str:
    """Send `text` to the user's Arcana chat session and return the reply.

    Creates the session + chat_store row on first call per user_id. Logs
    turns to chat_store so Layer-A replay / Layer-B distillation stay in
    sync. Caller is responsible for delivering the reply (text, voice, …).

    Sets the `current_tg_user` contextvar so Telegram-scoped tools (e.g.
    `switch_tts_voice`) can identify the caller without us threading
    user_id through every tool signature.
    """
    runtime = _get_runtime()
    if user_id not in _chat_sessions:
        personality = await _build_telegram_personality()
        _chat_sessions[user_id] = runtime.create_chat_session(
            system_prompt=personality
        )
        _history_ids[user_id] = await chat_store.create_session(
            source="telegram", label=str(user_id)
        )

    session = _chat_sessions[user_id]
    history_id = _history_ids.get(user_id)

    user_token = current_tg_user.set(user_id)
    origin_token = tool_guard.current_origin.set("telegram")
    try:
        if history_id:
            await chat_store.record_user(history_id, text)
        response = await session.send(text)
        reply = response.content or "(无回复)"
        if history_id:
            await chat_store.record_assistant(history_id, reply, 0)
            memory.record_turn_and_maybe_distill(history_id, runtime=runtime)
        return reply
    finally:
        tool_guard.current_origin.reset(origin_token)
        current_tg_user.reset(user_token)


async def _send_voice_reply(update: Update, reply: str) -> None:
    """Split the spoken portion of `reply` into chunks, synthesize in parallel,
    and send as sequential Telegram voice notes. Falls back silently if
    nothing is spoken-worthy or if TTS fails — the text reply is always sent
    separately by the caller, so the user never gets nothing."""
    spoken = extract_spoken_text(reply)
    if not spoken:
        return
    segments = segment_for_tts(spoken, max_chunks=3)
    if not segments:
        return

    user_id = update.effective_user.id if update.effective_user else None
    tasks = synthesize_segments_parallel(
        segments, voice=_resolve_tts_voice(user_id)
    )
    try:
        for i, task in enumerate(tasks):
            try:
                await update.message.chat.send_action("record_voice")
            except Exception:
                pass
            try:
                ogg = await task
            except Exception as e:
                logger.warning("tts segment %d failed: %s", i, e)
                continue
            try:
                await update.message.reply_voice(voice=ogg)
            except Exception as e:
                logger.warning("reply_voice %d failed: %s", i, e)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()


async def handle_message(update: Update, context) -> None:
    """Handle text messages — route through Arcana agent or send to session."""
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    text = update.message.text
    if not text:
        return

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

        await asyncio.sleep(1)

        ack = f"✅ 已发送到 <b>{escape(project)}</b>:\n<code>{escape(text)}</code>"
        await _safe_reply(update.message, ack)

        try:
            content_text, keyboard = await _build_session_content(session_id, project)
            await _safe_reply(update.message, content_text, reply_markup=keyboard)
        except Exception as e:
            logger.exception("build_session_content after send failed")
            await update.message.reply_text(f"(无法刷新会话视图: {e})")
        return

    # Normal chat flow — text in, text out.
    await update.message.chat.send_action("typing")
    try:
        reply = await _agent_reply(user_id, text)
    except Exception as e:
        logger.error(f"Agent error: {e}")
        await update.message.reply_text(f"出错了: {e}")
        return

    if len(reply) > 4000:
        reply = reply[:4000] + "\n\n...(截断)"
    await update.message.reply_text(reply)


async def handle_voice(update: Update, context) -> None:
    """Handle voice messages: download → whisper transcribe → agent → voice reply.

    The user hears a spoken answer (Edge TTS, 1-3 OGG/Opus chunks, parallel-
    synthesized for low first-bubble latency) plus the full text reply for
    reference. Awaiting-command mode still branches to send the transcribed
    text into the targeted iTerm2 session instead of the agent."""
    user_id = update.effective_user.id
    if not _is_allowed(user_id):
        return

    await update.message.chat.send_action("typing")

    voice = update.message.voice or update.message.audio
    if not voice:
        return

    file = await voice.get_file()
    ogg_path = f"/tmp/roboot_voice_{user_id}.ogg"
    await file.download_to_drive(ogg_path)

    try:
        stt = _get_stt_backend()
        if not stt.is_available():
            await update.message.reply_text(
                "语音识别不可用。\n"
                f"({stt.unavailable_reason()})"
            )
            return

        try:
            text = await stt.transcribe(ogg_path)
        except Exception as e:
            logger.exception("stt transcribe failed")
            await update.message.reply_text(f"转写失败: {e}")
            return

        if not text:
            await update.message.reply_text("没听清，再说一次？")
            return

        # Don't echo the transcript back as a text bubble — it clutters the
        # chat and the user already knows what they said. If the ASR was
        # wrong, the agent's reply will feel off-topic and they can re-ask.
        # Keep a log line for debugging.
        logger.info("voice transcript (user=%s): %s", user_id, text)

        # Awaiting-command: treat the transcript as terminal input for the
        # currently-selected session, same as a typed command.
        if _user_session_state.get(user_id, {}).get("awaiting_command"):
            update.message.text = text
            await handle_message(update, context)
            return

        try:
            reply = await _agent_reply(user_id, text)
        except Exception as e:
            logger.error(f"Agent error: {e}")
            await update.message.reply_text(f"出错了: {e}")
            return

        # Fire voice synthesis in parallel with sending the text reply so the
        # text lands quickly and the voice bubbles fill in as they're ready.
        voice_task = asyncio.create_task(_send_voice_reply(update, reply))

        text_reply = reply if len(reply) <= 4000 else reply[:4000] + "\n\n...(截断)"
        try:
            await update.message.reply_text(text_reply)
        except Exception as e:
            logger.warning("text reply failed: %s", e)

        await voice_task

    finally:
        try:
            os.unlink(ogg_path)
        except OSError:
            pass


# Set in main() once the Application is built; the tool_guard broadcaster
# reads this to know which bot to send through. Module-global instead of
# closure so tests can swap it for a stub without touching post_init.
_tg_app = None

# Bind req_id → triggering Telegram user_id so the callback handler can
# refuse cross-user clicks. The DM is targeted, but there is nothing in the
# `tool_ok:<req_id>` callback_data itself that ties it to one user — without
# this map any allowed user (or future second-device pairing) who learned
# a req_id could approve someone else's tool call. We rely on dict ops being
# atomic under the asyncio loop (no locks needed).
_pending_owner: dict[str, int] = {}


def _truncate_summary(text: str, limit: int = 600) -> str:
    """Inline-keyboard messages live in chat history forever. Cap the
    args_summary so a noisy 2KB command doesn't clog the user's chat —
    the audit file in `.tool_audit/` keeps the full record."""
    if len(text) <= limit:
        return text
    return text[:limit] + "...(截断)"


async def _broadcast_tool_approval(frame: dict) -> None:
    """tool_guard broadcaster for Telegram. DM the *triggering* user (read
    from the `current_tg_user` contextvar set in `_agent_reply`) with an
    inline keyboard. If the contextvar is unset — i.e. the tool fired
    outside a Telegram-driven chat turn — bail silently and let the gate
    time out (REJECTED). Better than spamming every allowed user.
    """
    app = _tg_app
    if app is None:
        logger.warning("tool_guard broadcaster: _tg_app is None, skipping")
        return
    user_id = current_tg_user.get(None)
    if user_id is None:
        logger.warning(
            "tool_guard broadcaster: no current_tg_user, skipping req_id=%s",
            frame.get("req_id"),
        )
        return
    req_id = frame.get("req_id", "")
    tool = frame.get("tool", "?")
    danger = str(frame.get("danger_reason") or "(无具体原因)")
    summary = _truncate_summary(str(frame.get("args_summary", "")))
    timeout_s = int(frame.get("timeout_s", 30))
    text = (
        "⚠️ <b>工具调用待批准</b>\n"
        f"工具: <code>{escape(tool)}</code>\n"
        f"原因: {escape(danger)}\n"
        f"超时: {timeout_s}s\n\n"
        f"<pre>{escape(summary)}</pre>"
    )
    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ 允许", callback_data=f"tool_ok:{req_id}"),
        InlineKeyboardButton("❌ 拒绝", callback_data=f"tool_no:{req_id}"),
    ]])
    # Bind owner BEFORE sending. If the send fails, we still fail closed —
    # the gate will time out at 30s, and the stale entry is cleaned up
    # below. The window where _pending_owner has an entry but the user has
    # no message to click on is harmless (no one can click).
    if req_id:
        _pending_owner[req_id] = user_id
    try:
        await app.bot.send_message(
            chat_id=user_id,
            text=text,
            parse_mode="HTML",
            reply_markup=keyboard,
        )
    except Exception as e:  # pragma: no cover - network dependent
        logger.warning("tool_approval send failed for %s: %s", user_id, e)
        _pending_owner.pop(req_id, None)


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
    """Wire post_init for the bot: subscribe the session watcher AND kick
    off STT model prewarm in the background.

    The telegram `Application` exposes `post_init`, which runs after the
    bot's loop is up. Registering the subscriber there guarantees
    `asyncio.create_task()` inside watcher.start() grabs the right loop.
    The prewarm is fire-and-forget — a first voice message that races
    the download just waits on the same HF cache slot, no duplicate work.
    """
    from session_watcher import watcher

    async def _post_init(_app):
        async def _cb(payload):
            await _notify_allowed_users(tg_app, payload)

        watcher.subscribe(_cb)
        watcher.start()

        # Idempotent — register_broadcaster dedupes, so a reload-style
        # double post_init wouldn't double-notify. Mirrors server.py.
        tool_guard.register_broadcaster(_broadcast_tool_approval)

        async def _prewarm_stt():
            try:
                backend = _get_stt_backend()
                logger.info("prewarming STT backend: %s", type(backend).__name__)
                await backend.prewarm()
            except Exception as e:
                logger.warning("STT prewarm failed: %s", e)

        asyncio.create_task(_prewarm_stt())

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
    # Stash for the tool_guard broadcaster — must be set before _post_init
    # registers the broadcaster, since the broadcaster reads this global
    # to know which bot to send through.
    global _tg_app
    _tg_app = app

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("screenshot", cmd_screenshot))
    app.add_handler(CommandHandler("voice", cmd_voice))
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
