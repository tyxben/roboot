"""Tests for adapters.voice_prefs — the tiny per-user TTS voice store
backing the `/voice` Telegram command. Uses tmp_path so the real
`.voice_prefs/` directory in the repo is never touched."""

from __future__ import annotations

import json

import pytest

from adapters import voice_prefs


@pytest.fixture(autouse=True)
def _tmp_prefs(tmp_path, monkeypatch):
    """Point voice_prefs at a fresh tmp file for every test."""
    voice_prefs.set_storage_path(tmp_path / "prefs.json")
    yield
    # Restore module default so tests don't leak state into each other.
    voice_prefs.set_storage_path(voice_prefs._DEFAULT_FILE)


def test_get_voice_missing_returns_none():
    assert voice_prefs.get_voice(42) is None


def test_set_then_get_roundtrip():
    voice_prefs.set_voice(42, "zh-CN-XiaoxiaoNeural")
    assert voice_prefs.get_voice(42) == "zh-CN-XiaoxiaoNeural"


def test_set_overwrites_existing():
    voice_prefs.set_voice(42, "zh-CN-YunxiNeural")
    voice_prefs.set_voice(42, "zh-CN-XiaoxiaoNeural")
    assert voice_prefs.get_voice(42) == "zh-CN-XiaoxiaoNeural"


def test_clear_removes_entry():
    voice_prefs.set_voice(42, "zh-CN-XiaoxiaoNeural")
    voice_prefs.clear(42)
    assert voice_prefs.get_voice(42) is None


def test_clear_nonexistent_is_noop():
    voice_prefs.clear(999)  # must not raise


def test_multiple_users_are_independent():
    voice_prefs.set_voice(1, "zh-CN-YunxiNeural")
    voice_prefs.set_voice(2, "zh-CN-XiaoxiaoNeural")
    assert voice_prefs.get_voice(1) == "zh-CN-YunxiNeural"
    assert voice_prefs.get_voice(2) == "zh-CN-XiaoxiaoNeural"


def test_corrupt_file_is_treated_as_empty(tmp_path, caplog):
    # Write garbage into the prefs file; next load should degrade gracefully.
    path = tmp_path / "corrupt-prefs.json"
    path.write_text("{not valid json")
    voice_prefs.set_storage_path(path)

    assert voice_prefs.get_voice(42) is None
    # Writing should still succeed (starts fresh).
    voice_prefs.set_voice(42, "zh-CN-YunxiNeural")
    assert json.loads(path.read_text()) == {"42": "zh-CN-YunxiNeural"}


def test_written_file_is_valid_json(tmp_path):
    path = tmp_path / "prefs.json"
    voice_prefs.set_storage_path(path)
    voice_prefs.set_voice(7, "zh-CN-XiaoyiNeural")
    loaded = json.loads(path.read_text())
    assert loaded == {"7": "zh-CN-XiaoyiNeural"}


def test_storage_path_directory_is_created(tmp_path):
    nested = tmp_path / "deeply" / "nested" / "prefs.json"
    voice_prefs.set_storage_path(nested)
    voice_prefs.set_voice(1, "zh-CN-YunxiNeural")
    assert nested.exists()
