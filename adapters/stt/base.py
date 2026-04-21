"""STT backend protocol.

Kept dependency-free so every adapter module can import it without
dragging in heavyweight ML or audio libraries.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class STTBackend(Protocol):
    async def transcribe(self, audio_path: str) -> str: ...

    def is_available(self) -> bool: ...

    def unavailable_reason(self) -> str: ...

    async def prewarm(self) -> None:
        """Do any expensive one-time setup (e.g. pre-cache model weights to
        disk) so the first real `transcribe()` call isn't a six-minute
        download. Safe to call repeatedly; no-op on backends that don't
        need warming. Callers should catch/log failures — prewarm must
        never prevent the bot from starting."""
        return None
