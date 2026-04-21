"""Apple-Silicon-native Whisper ASR via mlx-whisper.

Kept in its own module so `adapters/telegram_bot.py` and future voice
frontends can share it. The model repo (and therefore model size) is
configurable via env so users with less RAM can fall back to `medium` or
`small` without touching code.
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_MODEL_REPO = os.environ.get(
    "ROBOOT_WHISPER_MODEL", "mlx-community/whisper-large-v3-mlx"
)
DEFAULT_LANGUAGE = os.environ.get("ROBOOT_WHISPER_LANGUAGE", "zh")

_import_error: Exception | None = None
try:
    import mlx_whisper  # type: ignore
except Exception as e:  # pragma: no cover - env-dependent
    mlx_whisper = None  # type: ignore
    _import_error = e


def is_available() -> bool:
    return mlx_whisper is not None


def unavailable_reason() -> str:
    return f"mlx-whisper not importable: {_import_error}" if _import_error else ""


def _transcribe_sync(path: str, model_repo: str, language: str | None) -> str:
    result = mlx_whisper.transcribe(  # type: ignore[union-attr]
        path,
        path_or_hf_repo=model_repo,
        language=language,
        fp16=False,
    )
    return (result.get("text") or "").strip()


async def transcribe(
    audio_path: str | Path,
    *,
    model_repo: str = DEFAULT_MODEL_REPO,
    language: str | None = DEFAULT_LANGUAGE,
) -> str:
    """Transcribe an audio file (any ffmpeg-decodable format). Non-blocking."""
    if not is_available():
        raise RuntimeError(unavailable_reason() or "mlx_whisper unavailable")

    path = str(audio_path)
    logger.info("whisper transcribe start: %s (model=%s)", path, model_repo)
    text = await asyncio.to_thread(_transcribe_sync, path, model_repo, language)
    logger.info("whisper transcribe done (%d chars)", len(text))
    return text
