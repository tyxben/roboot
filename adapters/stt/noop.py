"""No-op STT backend — used when the user has explicitly disabled voice input."""

from __future__ import annotations


_REASON = "STT disabled via config (voice.stt.backend = none)"


class NoopBackend:
    def __init__(
        self,
        model: str | None = None,
        language: str | None = None,
    ) -> None:
        pass

    def is_available(self) -> bool:
        return False

    def unavailable_reason(self) -> str:
        return _REASON

    async def transcribe(self, audio_path: str) -> str:
        raise RuntimeError(_REASON)

    async def prewarm(self) -> None:
        return None
