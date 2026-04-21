"""Tests for the pluggable STT backend factory in adapters/stt."""

from __future__ import annotations

import builtins
import logging
import sys

import pytest

from adapters import stt as stt_pkg
from adapters.stt import (
    GoogleSTTBackend,
    MlxWhisperBackend,
    NoopBackend,
    get_backend,
    reset_cache,
)


@pytest.fixture(autouse=True)
def _clear_cache():
    reset_cache()
    yield
    reset_cache()


def test_default_backend_is_mlx_whisper():
    backend = get_backend({})
    assert isinstance(backend, MlxWhisperBackend)


def test_google_backend_selected_by_config():
    backend = get_backend({"voice": {"stt": {"backend": "google"}}})
    assert isinstance(backend, GoogleSTTBackend)


async def test_none_backend_is_unavailable_and_raises():
    backend = get_backend({"voice": {"stt": {"backend": "none"}}})
    assert isinstance(backend, NoopBackend)
    assert backend.is_available() is False
    assert "disabled" in backend.unavailable_reason().lower()

    with pytest.raises(RuntimeError):
        await backend.transcribe("/tmp/whatever.ogg")


def test_unknown_backend_falls_back_to_mlx_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger="adapters.stt"):
        backend = get_backend({"voice": {"stt": {"backend": "banana"}}})
    assert isinstance(backend, MlxWhisperBackend)
    assert any("Unknown STT backend" in r.message for r in caplog.records)


def test_factory_caches_default_instance():
    a = get_backend()
    b = get_backend()
    assert a is b
    reset_cache()
    c = get_backend()
    assert c is not a


def test_google_backend_is_unavailable_without_speech_recognition(monkeypatch):
    # Drop any already-imported copy so our __import__ hook is what runs.
    monkeypatch.delitem(sys.modules, "speech_recognition", raising=False)

    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "speech_recognition" or name.startswith("speech_recognition."):
            raise ImportError("simulated missing dep")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)

    backend = GoogleSTTBackend()
    assert backend.is_available() is False
    assert "SpeechRecognition" in backend.unavailable_reason()


def test_mlx_backend_honors_explicit_model_and_language():
    backend = MlxWhisperBackend(model="mlx-community/whisper-small", language="en")
    assert backend.model == "mlx-community/whisper-small"
    assert backend.language == "en"


def test_get_backend_exposes_reset_cache_module_level():
    # Sanity: the package re-exports reset_cache + get_backend.
    assert callable(stt_pkg.get_backend)
    assert callable(stt_pkg.reset_cache)


async def test_noop_prewarm_is_safe():
    backend = get_backend({"voice": {"stt": {"backend": "none"}}})
    await backend.prewarm()  # must not raise


async def test_google_prewarm_is_safe_even_when_unavailable(monkeypatch):
    monkeypatch.delitem(sys.modules, "speech_recognition", raising=False)
    real_import = builtins.__import__

    def fake_import(name, globals=None, locals=None, fromlist=(), level=0):
        if name == "speech_recognition":
            raise ImportError("simulated missing dep")
        return real_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    backend = GoogleSTTBackend()
    await backend.prewarm()  # logs but does not raise


async def test_mlx_prewarm_invokes_snapshot_download(monkeypatch):
    """The mlx backend pre-caches weights by calling `snapshot_download`
    directly — no RAM load — so an idle bot stays lean until someone
    actually sends a voice message."""
    called_with: dict[str, str] = {}

    def fake_snapshot_download(*, repo_id: str, **_: object) -> str:
        called_with["repo_id"] = repo_id
        return "/tmp/fake-snapshot"

    import types
    fake_hf = types.ModuleType("huggingface_hub")
    fake_hf.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    backend = MlxWhisperBackend(model="mlx-community/whisper-tiny")
    if not backend.is_available():
        pytest.skip("mlx_whisper not importable in this env")

    await backend.prewarm()
    assert called_with.get("repo_id") == "mlx-community/whisper-tiny"


async def test_mlx_prewarm_swallows_download_errors(monkeypatch, caplog):
    """Network-down at setup time must not prevent the bot from starting;
    the user gets a clear error on their first voice message instead."""
    def fake_snapshot_download(*, repo_id: str, **_: object) -> str:
        raise RuntimeError("network is down")

    import types
    fake_hf = types.ModuleType("huggingface_hub")
    fake_hf.snapshot_download = fake_snapshot_download  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "huggingface_hub", fake_hf)

    backend = MlxWhisperBackend(model="mlx-community/whisper-tiny")
    if not backend.is_available():
        pytest.skip("mlx_whisper not importable in this env")

    with caplog.at_level(logging.WARNING, logger="adapters.stt.mlx"):
        await backend.prewarm()  # must not raise
    assert any("prewarm failed" in r.message for r in caplog.records)
