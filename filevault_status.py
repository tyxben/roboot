"""macOS FileVault status probe.

The rest of Roboot's at-rest security model (config.yaml with API keys,
`.identity/daemon.ed25519.key`, `.chat_history.db`, `soul.md`, faces DB —
all plaintext on disk) assumes the user's boot volume is FileVault-encrypted.
If it isn't, a laptop-theft scenario reads everything. The console surfaces
a red banner when we detect FileVault is off so the assumption isn't silent.

`fdesetup status` is the supported macOS command for this. It prints
"FileVault is On." or "FileVault is Off." on stdout. No sudo required.
"""

from __future__ import annotations

import asyncio
import sys


async def check() -> dict:
    """Return {"enabled": bool|None, "platform": str, "error"?: str}.

    `enabled=None` means "unknown" — non-macOS hosts or a probe failure.
    Callers (the console banner) should only alarm on `enabled=False`.
    """
    if sys.platform != "darwin":
        return {"enabled": None, "platform": sys.platform}
    try:
        proc = await asyncio.create_subprocess_exec(
            "fdesetup",
            "status",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        except asyncio.TimeoutError:
            proc.kill()
            return {"enabled": None, "platform": "darwin", "error": "timeout"}
        out = stdout.decode(errors="replace")
        if "FileVault is On" in out:
            return {"enabled": True, "platform": "darwin"}
        if "FileVault is Off" in out:
            return {"enabled": False, "platform": "darwin"}
        return {"enabled": None, "platform": "darwin", "error": "unknown_output"}
    except FileNotFoundError:
        return {"enabled": None, "platform": "darwin", "error": "fdesetup_missing"}
    except Exception as e:
        return {"enabled": None, "platform": "darwin", "error": str(e)[:80]}
