"""Keyboard I/O adapter — always available fallback."""

from __future__ import annotations


class KeyboardIO:
    """Simple stdin/stdout adapter."""

    async def listen(self) -> str | None:
        """Read a line from stdin. Returns None on EOF."""
        try:
            text = input("你: ").strip()
            return text or None
        except (EOFError, KeyboardInterrupt):
            return None

    async def speak(self, text: str) -> None:
        """Print to stdout (no TTS)."""
        print(f"Roboot: {text}")
