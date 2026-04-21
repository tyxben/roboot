"""CLI: `python -m adapters.stt prewarm`

Downloads the configured STT backend's model weights so the first real
voice message doesn't have to wait 6 minutes on a cold HuggingFace
fetch. Safe to run during install / provisioning / Dockerfile builds.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from . import get_backend


def _usage() -> int:
    sys.stderr.write(
        "usage: python -m adapters.stt <command>\n"
        "\n"
        "commands:\n"
        "  prewarm   download model weights for the configured backend\n"
        "  info      print the selected backend and whether it's ready\n"
    )
    return 2


async def _prewarm() -> int:
    backend = get_backend()
    print(f"STT backend: {type(backend).__name__}")
    if not backend.is_available():
        print(f"  not available: {backend.unavailable_reason()}")
        print("  (prewarm may still pre-cache weights — continuing)")
    await backend.prewarm()
    print("done.")
    return 0


def _info() -> int:
    backend = get_backend()
    print(f"STT backend: {type(backend).__name__}")
    print(f"  available: {backend.is_available()}")
    if not backend.is_available():
        print(f"  reason: {backend.unavailable_reason()}")
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    if len(sys.argv) != 2:
        return _usage()
    cmd = sys.argv[1]
    if cmd == "prewarm":
        return asyncio.run(_prewarm())
    if cmd == "info":
        return _info()
    return _usage()


if __name__ == "__main__":
    raise SystemExit(main())
