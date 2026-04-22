"""Edge TTS synthesis helper shared by the REST /api/tts endpoint
(local console) and the relay tts_request handler (mobile pair-page).

Both surfaces want the same behavior — extract the spoken portion of a
reply (`> `-prefixed lines or first-sentence fallback), pick the active
voice (per-user override → global config default → hard-coded default),
and return MP3 bytes. Keeping it here stops the two paths from drifting.
"""

from __future__ import annotations

import logging

import edge_tts

from text_utils import extract_spoken_text
from tools.soul import get_voice

logger = logging.getLogger(__name__)

TTS_VOICE_DEFAULT = "zh-CN-YunxiNeural"
TTS_RATE = "+10%"


async def _edge_tts_once(text: str, voice: str) -> bytes:
    comm = edge_tts.Communicate(text, voice=voice, rate=TTS_RATE)
    out = bytearray()
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            out.extend(chunk["data"])
    return bytes(out)


async def synthesize_spoken(raw_text: str, voice: str | None = None) -> bytes:
    """Extract spoken text from `raw_text` and synthesize MP3 via Edge TTS.

    Returns empty bytes if there is nothing to speak. One silent retry on
    transient failures (Edge TTS connection resets sometimes); the mobile
    path is latency-sensitive so we don't do exponential backoff here.
    """
    text = extract_spoken_text(raw_text)
    if not text:
        return b""
    chosen = voice or get_voice() or TTS_VOICE_DEFAULT
    try:
        return await _edge_tts_once(text, chosen)
    except Exception as e:
        logger.warning("edge_tts first attempt failed: %s; retrying once", e)
        return await _edge_tts_once(text, chosen)
