"""Tests for tts_synth.synthesize_spoken — the shared helper used by both
`/api/tts` (local console) and the relay's tts_request handler (mobile).

The actual Edge TTS call is network-dependent, so we monkeypatch the
internal `_edge_tts_once` helper to avoid flaky CI. Focus of these tests:

- Empty / whitespace-only input returns b"" without calling Edge TTS.
- Spoken extraction is applied (lines prefixed with "> " are kept).
- Voice selection precedence: explicit arg > get_voice() > default.
- One silent retry on transient failure; second failure propagates.
"""

from __future__ import annotations

import pytest

import tts_synth


@pytest.fixture(autouse=True)
def _reset_voice(monkeypatch):
    # Default: no per-user voice override, so the fallback chain hits the
    # hard-coded default unless a test overrides explicitly.
    monkeypatch.setattr(tts_synth, "get_voice", lambda: None)


async def test_empty_text_returns_empty_bytes(monkeypatch):
    called = {"n": 0}

    async def fake(_text, _voice):
        called["n"] += 1
        return b"should not be called"

    monkeypatch.setattr(tts_synth, "_edge_tts_once", fake)
    assert await tts_synth.synthesize_spoken("") == b""
    assert await tts_synth.synthesize_spoken("   \n\n  ") == b""
    assert called["n"] == 0


async def test_spoken_extraction_is_applied(monkeypatch):
    captured = {}

    async def fake(text, voice):
        captured["text"] = text
        captured["voice"] = voice
        return b"mp3data"

    monkeypatch.setattr(tts_synth, "_edge_tts_once", fake)

    reply = "Here is a long analysis paragraph that should NOT be spoken.\n> 你好，我是JARVIS。\n> 今天天气不错。\nMore prose."
    out = await tts_synth.synthesize_spoken(reply)
    assert out == b"mp3data"
    assert "你好" in captured["text"]
    assert "long analysis paragraph" not in captured["text"]


async def test_explicit_voice_overrides_default(monkeypatch):
    captured = {}

    async def fake(text, voice):
        captured["voice"] = voice
        return b"x"

    monkeypatch.setattr(tts_synth, "_edge_tts_once", fake)
    monkeypatch.setattr(tts_synth, "get_voice", lambda: "zh-CN-XiaoxiaoNeural")

    await tts_synth.synthesize_spoken("> 你好", voice="en-US-JennyNeural")
    assert captured["voice"] == "en-US-JennyNeural"


async def test_get_voice_used_when_no_explicit_arg(monkeypatch):
    captured = {}

    async def fake(text, voice):
        captured["voice"] = voice
        return b"x"

    monkeypatch.setattr(tts_synth, "_edge_tts_once", fake)
    monkeypatch.setattr(tts_synth, "get_voice", lambda: "zh-CN-XiaoxiaoNeural")

    await tts_synth.synthesize_spoken("> 你好")
    assert captured["voice"] == "zh-CN-XiaoxiaoNeural"


async def test_hardcoded_default_when_nothing_configured(monkeypatch):
    captured = {}

    async def fake(text, voice):
        captured["voice"] = voice
        return b"x"

    monkeypatch.setattr(tts_synth, "_edge_tts_once", fake)
    # get_voice returns None from the autouse fixture.

    await tts_synth.synthesize_spoken("> 你好")
    assert captured["voice"] == tts_synth.TTS_VOICE_DEFAULT


async def test_retries_once_on_transient_failure(monkeypatch):
    attempts = {"n": 0}

    async def flaky(_text, _voice):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionResetError("simulated edge-tts drop")
        return b"recovered"

    monkeypatch.setattr(tts_synth, "_edge_tts_once", flaky)
    out = await tts_synth.synthesize_spoken("> 你好")
    assert out == b"recovered"
    assert attempts["n"] == 2


async def test_second_failure_propagates(monkeypatch):
    async def always_fails(_text, _voice):
        raise RuntimeError("edge-tts is dead")

    monkeypatch.setattr(tts_synth, "_edge_tts_once", always_fails)
    with pytest.raises(RuntimeError):
        await tts_synth.synthesize_spoken("> 你好")
