"""Apple-Silicon-native Whisper ASR via mlx-whisper."""

from __future__ import annotations

import asyncio
import logging
import os

logger = logging.getLogger(__name__)

DEFAULT_MODEL_REPO = "mlx-community/whisper-large-v3-mlx"
DEFAULT_LANGUAGE = "zh"


class MlxWhisperBackend:
    """Whisper ASR using Apple's MLX framework.

    Arm64 macOS only in practice — the import is done lazily so importing
    this module never crashes on Intel Macs or Linux.
    """

    def __init__(
        self,
        model: str | None = None,
        language: str | None = None,
    ) -> None:
        self.model = (
            model
            or os.environ.get("ROBOOT_WHISPER_MODEL")
            or DEFAULT_MODEL_REPO
        )
        self.language = (
            language
            or os.environ.get("ROBOOT_WHISPER_LANGUAGE")
            or DEFAULT_LANGUAGE
        )

        self._import_error: Exception | None = None
        try:
            import mlx_whisper  # type: ignore

            self._mlx_whisper = mlx_whisper
        except Exception as e:  # pragma: no cover - env-dependent
            self._mlx_whisper = None
            self._import_error = e

    def is_available(self) -> bool:
        return self._mlx_whisper is not None

    def unavailable_reason(self) -> str:
        if self._import_error is not None:
            return f"mlx-whisper not importable: {self._import_error}"
        return ""

    def _transcribe_sync(self, path: str) -> str:
        result = self._mlx_whisper.transcribe(  # type: ignore[union-attr]
            path,
            path_or_hf_repo=self.model,
            language=self.language,
            fp16=False,
        )
        return (result.get("text") or "").strip()

    async def transcribe(self, audio_path: str) -> str:
        if not self.is_available():
            raise RuntimeError(
                self.unavailable_reason() or "mlx_whisper unavailable"
            )
        path = str(audio_path)
        logger.info("whisper transcribe start: %s (model=%s)", path, self.model)
        text = await asyncio.to_thread(self._transcribe_sync, path)
        logger.info("whisper transcribe done (%d chars)", len(text))
        return text
