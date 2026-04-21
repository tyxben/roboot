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

    async def prewarm(self) -> None:
        """Ensure the model weights are cached on disk without loading them
        into RAM. Idempotent: HuggingFace Hub resumes partial downloads and
        skips files that are already fully present.

        We call `snapshot_download` directly rather than doing a dummy
        `transcribe()` so idle RAM stays flat until a real voice message
        arrives — otherwise a bot that nobody's talking to would sit on
        ~2 GB of model weights forever."""
        if not self.is_available():
            logger.info(
                "skipping whisper prewarm: %s", self.unavailable_reason()
            )
            return
        try:
            from huggingface_hub import snapshot_download  # type: ignore
        except Exception as e:
            logger.warning("huggingface_hub unavailable; cannot prewarm: %s", e)
            return

        logger.info("whisper prewarm: caching %s to disk", self.model)

        def _download() -> None:
            snapshot_download(repo_id=self.model)

        try:
            await asyncio.to_thread(_download)
            logger.info("whisper prewarm done: %s", self.model)
        except Exception as e:
            # Network down during setup shouldn't kill the bot; the user
            # will see a clear error on their first voice message instead.
            logger.warning("whisper prewarm failed (%s): %s", self.model, e)
