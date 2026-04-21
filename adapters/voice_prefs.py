"""Per-user TTS voice preferences.

Persists `telegram_user_id → edge-tts voice name` to
`.voice_prefs/prefs.json` at the repo root so each Telegram user can
pick their own voice with `/voice` without us touching the shared
config.yaml.

Intentionally tiny: the file is rewritten atomically (tempfile +
os.replace) on every mutation — for a handful of users this beats
carrying a lock around, and atomic replace is safe against partial
writes if the process crashes mid-update.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from pathlib import Path

logger = logging.getLogger(__name__)

_DEFAULT_DIR = Path(__file__).resolve().parent.parent / ".voice_prefs"
_DEFAULT_FILE = _DEFAULT_DIR / "prefs.json"

_prefs_path: Path = _DEFAULT_FILE


def set_storage_path(path: Path) -> None:
    """Override the storage location (for tests)."""
    global _prefs_path
    _prefs_path = Path(path)


def _load() -> dict[str, str]:
    try:
        raw = _prefs_path.read_text()
    except FileNotFoundError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.warning("voice_prefs: corrupt %s (%s); starting fresh", _prefs_path, e)
        return {}
    return data if isinstance(data, dict) else {}


def _save(prefs: dict[str, str]) -> None:
    _prefs_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(
        prefix="prefs.", suffix=".tmp", dir=str(_prefs_path.parent)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(prefs, f, ensure_ascii=False, indent=2)
        os.replace(tmp, _prefs_path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def get_voice(user_id: int) -> str | None:
    return _load().get(str(user_id))


def set_voice(user_id: int, voice: str) -> None:
    prefs = _load()
    prefs[str(user_id)] = voice
    _save(prefs)


def clear(user_id: int) -> None:
    prefs = _load()
    if str(user_id) in prefs:
        del prefs[str(user_id)]
        _save(prefs)
