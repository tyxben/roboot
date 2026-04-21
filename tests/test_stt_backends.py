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
