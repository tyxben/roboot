"""TTS segmentation + parallel Edge-TTS → OGG/Opus synthesis.

Designed for Telegram voice replies: the bot splits the spoken text into a
small number of chunks, synthesizes them in parallel, and sends them as
sequential `reply_voice` bubbles so the user hears the first sentence while
the rest are still being generated.

OGG/Opus is Telegram's native voice-note format — sending anything else
shows up as a music file instead of a voice bubble.
"""

from __future__ import annotations

import asyncio
import logging
import re

import edge_tts

logger = logging.getLogger(__name__)

DEFAULT_VOICE = "zh-CN-YunxiNeural"
DEFAULT_RATE = "+10%"

_SENTENCE_END = re.compile(r"([。！？!?]+|[.]+(?=\s|$))")
_MIN_CHUNK_CHARS = 25
_SHORT_TEXT_THRESHOLD = 40


def segment_for_tts(text: str, max_chunks: int = 3) -> list[str]:
    """Split `text` into at most `max_chunks` chunks at sentence boundaries.

    Short fragments (<25 chars) are merged forward so users don't get a burst
    of tiny voice bubbles. If the splitter produces more than `max_chunks`
    segments, everything beyond the cap is folded into the final chunk.
    """
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= _SHORT_TEXT_THRESHOLD:
        return [text]

    parts = _SENTENCE_END.split(text)
    sentences: list[str] = []
    buf = ""
    for piece in parts:
        if not piece:
            continue
        buf += piece
        if _SENTENCE_END.fullmatch(piece):
            s = buf.strip()
            if s:
                sentences.append(s)
            buf = ""
    tail = buf.strip()
    if tail:
        sentences.append(tail)

    if not sentences:
        return [text]

    merged: list[str] = []
    for s in sentences:
        if merged and len(merged[-1]) < _MIN_CHUNK_CHARS:
            merged[-1] = (merged[-1] + s).strip()
        else:
            merged.append(s)

    if len(merged) > max_chunks:
        head = merged[: max_chunks - 1]
        tail_combined = " ".join(merged[max_chunks - 1 :]).strip()
        merged = head + [tail_combined]

    return [m for m in merged if m]


async def _edge_tts_mp3(text: str, voice: str, rate: str, *, attempts: int = 3) -> bytes:
    """Edge TTS is occasionally flaky under concurrent connections (connection
    reset / NoAudioReceived). Retry with exponential backoff so one bad
    handshake doesn't silence a voice reply."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            comm = edge_tts.Communicate(text, voice=voice, rate=rate)
            audio = bytearray()
            async for chunk in comm.stream():
                if chunk["type"] == "audio":
                    audio.extend(chunk["data"])
            if audio:
                return bytes(audio)
            raise RuntimeError("edge_tts returned empty audio")
        except Exception as e:
            last_exc = e
            logger.warning("edge_tts attempt %d/%d failed: %s", i + 1, attempts, e)
            if i < attempts - 1:
                await asyncio.sleep(0.4 * (2**i))
    raise RuntimeError(f"edge_tts failed after {attempts} attempts: {last_exc}")


async def _mp3_to_ogg_opus(mp3: bytes) -> bytes:
    """Pipe mp3 bytes through ffmpeg → OGG/Opus 48kHz mono 32kbps.

    Telegram voice notes require OGG/Opus. 32kbps is the Telegram client
    default and keeps files small over mobile networks without audible loss
    on speech.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-c:a",
        "libopus",
        "-b:a",
        "32k",
        "-ar",
        "48000",
        "-ac",
        "1",
        "-f",
        "ogg",
        "pipe:1",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate(mp3)
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg mp3→ogg failed ({proc.returncode}): {stderr.decode('utf-8', 'replace')[:200]}"
        )
    return stdout


async def synthesize_ogg(
    text: str,
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
) -> bytes:
    """Synthesize one chunk of text to OGG/Opus bytes ready for send_voice."""
    mp3 = await _edge_tts_mp3(text, voice, rate)
    return await _mp3_to_ogg_opus(mp3)


async def _staggered_synth(text: str, voice: str, rate: str, delay: float) -> bytes:
    if delay > 0:
        await asyncio.sleep(delay)
    return await synthesize_ogg(text, voice=voice, rate=rate)


def synthesize_segments_parallel(
    segments: list[str],
    voice: str = DEFAULT_VOICE,
    rate: str = DEFAULT_RATE,
    stagger: float = 0.15,
) -> list[asyncio.Task[bytes]]:
    """Kick off parallel synthesis; caller awaits tasks in order to preserve
    bubble ordering while still benefiting from concurrent generation.

    A small stagger between handshakes avoids Edge-TTS connection resets that
    occasionally happen when multiple websockets open simultaneously.
    """
    return [
        asyncio.create_task(_staggered_synth(s, voice, rate, stagger * i))
        for i, s in enumerate(segments)
    ]
