"""Google Web Speech backend (via the SpeechRecognition library).

Works cross-platform without a heavy local model, but needs the network
and ffmpeg on PATH to transcode Telegram's OGG/Opus voice notes into
16 kHz mono WAV that `recognize_google` can consume.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile

logger = logging.getLogger(__name__)

DEFAULT_LANGUAGE = "zh"

# SpeechRecognition wants BCP-47 codes; our config uses short codes.
_LANG_MAP = {
    "zh": "zh-CN",
    "en": "en-US",
}


class GoogleSTTBackend:
    def __init__(
        self,
        model: str | None = None,  # unused, kept for a uniform constructor
        language: str | None = None,
    ) -> None:
        lang = (language or DEFAULT_LANGUAGE).strip()
        self.language = _LANG_MAP.get(lang, lang)

    def _speech_recognition_importable(self) -> bool:
        try:
            import speech_recognition  # noqa: F401
            return True
        except Exception:
            return False

    def is_available(self) -> bool:
        return (
            self._speech_recognition_importable()
            and shutil.which("ffmpeg") is not None
        )

    def unavailable_reason(self) -> str:
        if not self._speech_recognition_importable():
            return (
                "SpeechRecognition not importable: "
                "install with `pip install SpeechRecognition`"
            )
        if shutil.which("ffmpeg") is None:
            return "ffmpeg not found on PATH (required to decode voice notes)"
        return ""

    async def transcribe(self, audio_path: str) -> str:
        try:
            import speech_recognition as sr  # type: ignore
        except Exception as e:
            raise RuntimeError(
                f"SpeechRecognition not importable: {e}"
            ) from e

        if shutil.which("ffmpeg") is None:
            raise RuntimeError(
                "ffmpeg not found on PATH (required to decode voice notes)"
            )

        # ffmpeg: OGG/Opus (or anything) → 16 kHz mono s16 WAV.
        fd, wav_path = tempfile.mkstemp(suffix=".wav", prefix="roboot_stt_")
        os.close(fd)

        try:
            proc = await asyncio.create_subprocess_exec(
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(audio_path),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "wav",
                wav_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg failed ({proc.returncode}): "
                    f"{stderr.decode(errors='replace').strip()}"
                )

            def _recognize() -> str:
                recognizer = sr.Recognizer()
                with sr.AudioFile(wav_path) as source:
                    audio = recognizer.record(source)
                return recognizer.recognize_google(audio, language=self.language) or ""

            logger.info("google stt start: %s (lang=%s)", audio_path, self.language)
            text = await asyncio.to_thread(_recognize)
            text = (text or "").strip()
            logger.info("google stt done (%d chars)", len(text))
            return text
        finally:
            try:
                os.unlink(wav_path)
            except OSError:
                pass
