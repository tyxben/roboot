"""Arcana tool: switch the Telegram user's TTS voice.

The tool reads the current user's Telegram ID from a contextvar that the
bot sets at the top of each agent reply (`_current_tg_user` in
`adapters/telegram_bot.py`). Non-Telegram callers hit the contextvar's
default of `None` and the tool returns a friendly no-op — nothing breaks,
the agent just can't use this in the web console.
"""

from __future__ import annotations

import contextvars

import arcana

from adapters import voice_prefs


current_tg_user: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "current_tg_user", default=None
)


# Curated list mirrors the `/voice` picker. Keep in sync: if the Telegram
# picker adds or removes a voice, update here too (or extract to a shared
# constant — for now the surface area is small enough that duplication is
# cheaper than a new module import chain into arcana tool specs).
_VOICES: dict[str, str] = {
    "zh-CN-YunxiNeural": "云希 男声沉稳（默认）",
    "zh-CN-YunjianNeural": "云健 男声深沉",
    "zh-CN-YunyangNeural": "云扬 男声新闻",
    "zh-CN-YunxiaNeural": "云夏 男童声",
    "zh-CN-XiaoxiaoNeural": "晓晓 女声温暖",
    "zh-CN-XiaoyiNeural": "晓伊 女声活泼",
    "zh-CN-liaoning-XiaobeiNeural": "晓贝 东北话",
    "zh-CN-shaanxi-XiaoniNeural": "晓妮 陕西话",
    "en-US-JennyNeural": "Jenny EN female",
    "en-US-GuyNeural": "Guy EN male",
}

_WHEN_TO_USE = (
    "当 Telegram 用户要求切换朗读声音时（如\"换成女声\"、\"用东北话\"、"
    "\"换个男声\"、\"用英语说话\"）。"
    "可用 voice 值:\n"
    + "\n".join(f"  {name} — {desc}" for name, desc in _VOICES.items())
    + "\n"
    "只在 Telegram 渠道有效。其它渠道调用会返回说明。"
)


@arcana.tool(
    when_to_use=_WHEN_TO_USE,
    what_to_expect="确认切换成功的简短中文字符串；下一条语音回复会用新声音。",
    side_effect="write",
)
async def switch_tts_voice(voice: str) -> str:
    """Switch the current Telegram user's TTS voice preference.

    `voice` must be a full edge-tts voice identifier (e.g.
    `zh-CN-XiaoxiaoNeural`). Prefer the names in the when_to_use list
    above — unknown names are rejected to avoid a silent no-op.
    """
    voice = (voice or "").strip()
    if not voice:
        return "请提供 voice 参数，比如 zh-CN-XiaoxiaoNeural。"
    if voice not in _VOICES:
        valid = ", ".join(_VOICES.keys())
        return (
            f"不支持 {voice}。支持的声音: {valid}。"
            "也可以让用户用 /voice 打开选择菜单。"
        )
    user_id = current_tg_user.get()
    if user_id is None:
        return "这个工具只在 Telegram 渠道能用（当前渠道没有 user_id）。"
    voice_prefs.set_voice(user_id, voice)
    label = _VOICES[voice]
    return f"好的，已经切换到 {label}（{voice}）。下条语音就用新声音。"
