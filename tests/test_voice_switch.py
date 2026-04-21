"""Tests for tools.voice_switch — the Arcana tool the agent calls when the
user asks to change the TTS voice. Exercises the contextvar-scoped
behavior so we know the tool is safe to call outside Telegram."""

from __future__ import annotations

import pytest

from adapters import voice_prefs
from tools import voice_switch

# @arcana.tool() only attaches metadata (`_arcana_tool_spec`); the name still
# refers to a regular async function, so tests can call it directly.
_switch = voice_switch.switch_tts_voice


@pytest.fixture(autouse=True)
def _tmp_prefs(tmp_path):
    voice_prefs.set_storage_path(tmp_path / "prefs.json")
    token = voice_switch.current_tg_user.set(None)
    yield
    voice_switch.current_tg_user.reset(token)
    voice_prefs.set_storage_path(voice_prefs._DEFAULT_FILE)


async def test_rejects_without_telegram_context():
    # No user_id → tool must refuse rather than write to a mystery slot.
    result = await _switch("zh-CN-XiaoxiaoNeural")
    assert "Telegram" in result


async def test_rejects_unknown_voice():
    token = voice_switch.current_tg_user.set(42)
    try:
        result = await _switch("fr-FR-BogusNeural")
    finally:
        voice_switch.current_tg_user.reset(token)
    assert "不支持" in result
    # Should NOT have written.
    assert voice_prefs.get_voice(42) is None


async def test_rejects_empty_voice():
    token = voice_switch.current_tg_user.set(42)
    try:
        result = await _switch("")
    finally:
        voice_switch.current_tg_user.reset(token)
    assert "voice" in result.lower() or "zh-CN" in result


async def test_valid_switch_writes_prefs():
    token = voice_switch.current_tg_user.set(42)
    try:
        result = await _switch("zh-CN-XiaoxiaoNeural")
    finally:
        voice_switch.current_tg_user.reset(token)
    assert "晓晓" in result
    assert voice_prefs.get_voice(42) == "zh-CN-XiaoxiaoNeural"


async def test_switch_is_per_user():
    # Two users change voice independently.
    for uid, voice in [(1, "zh-CN-YunxiNeural"), (2, "zh-CN-XiaoxiaoNeural")]:
        token = voice_switch.current_tg_user.set(uid)
        try:
            await _switch(voice)
        finally:
            voice_switch.current_tg_user.reset(token)
    assert voice_prefs.get_voice(1) == "zh-CN-YunxiNeural"
    assert voice_prefs.get_voice(2) == "zh-CN-XiaoxiaoNeural"
