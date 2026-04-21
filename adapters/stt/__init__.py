"""Pluggable speech-to-text backends.

Selection happens via `voice.stt.backend` in config.yaml:

    voice:
      stt:
        backend: mlx_whisper        # options: mlx_whisper | google | none
        model: whisper-large-v3-mlx # optional, mlx_whisper-only
        language: zh                # optional, default "zh"

`get_backend()` resolves the config, constructs the backend once, and
caches it at module scope. Tests can call `reset_cache()` to force a
re-read.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from .base import STTBackend
from .google import GoogleSTTBackend
from .mlx import MlxWhisperBackend
from .noop import NoopBackend

__all__ = [
    "STTBackend",
    "GoogleSTTBackend",
    "MlxWhisperBackend",
    "NoopBackend",
    "get_backend",
    "reset_cache",
]

logger = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_CONFIG_PATH = _REPO_ROOT / "config.yaml"
_EXAMPLE_CONFIG_PATH = _REPO_ROOT / "config.example.yaml"

_BACKENDS: dict[str, type] = {
    "mlx_whisper": MlxWhisperBackend,
    "google": GoogleSTTBackend,
    "none": NoopBackend,
}

_cached_backend: STTBackend | None = None


def _load_config_from_disk() -> dict[str, Any]:
    for path in (_CONFIG_PATH, _EXAMPLE_CONFIG_PATH):
        try:
            raw = path.read_text()
        except FileNotFoundError:
            continue
        try:
            return yaml.safe_load(raw) or {}
        except yaml.YAMLError as e:
            logger.warning("Failed to parse %s (%s); using STT defaults", path, e)
            return {}
    return {}


def get_backend(config: dict[str, Any] | None = None) -> STTBackend:
    """Return the STT backend selected by config.

    With no argument, reads `config.yaml` (fallback `config.example.yaml`)
    once and caches the result.
    """
    global _cached_backend
    if config is None:
        if _cached_backend is not None:
            return _cached_backend
        config = _load_config_from_disk()
        backend = _build_backend(config)
        _cached_backend = backend
        return backend
    return _build_backend(config)


def _build_backend(config: dict[str, Any]) -> STTBackend:
    stt_cfg = ((config or {}).get("voice") or {}).get("stt") or {}
    name = (stt_cfg.get("backend") or "mlx_whisper").strip()
    cls = _BACKENDS.get(name)
    if cls is None:
        logger.warning(
            "Unknown STT backend %r; falling back to mlx_whisper", name
        )
        cls = MlxWhisperBackend
    return cls(
        model=stt_cfg.get("model"),
        language=stt_cfg.get("language"),
    )


def reset_cache() -> None:
    """Clear the cached backend. Primarily for tests."""
    global _cached_backend
    _cached_backend = None
