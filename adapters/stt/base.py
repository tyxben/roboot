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
