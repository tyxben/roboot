"""Smoke tests for filevault_status.check().

We don't actually run `fdesetup`; we swap in a fake `asyncio.create_subprocess_exec`
that returns scripted stdout. The module parses stdout for "FileVault is On/Off"
and maps failures to `enabled=None`.
"""

from __future__ import annotations

import asyncio

import pytest

import filevault_status as fv


class _FakeProc:
    def __init__(self, stdout: bytes):
        self._stdout = stdout

    async def communicate(self):
        return self._stdout, b""

    def kill(self):  # pragma: no cover - only used in timeout path
        pass


def _patch_darwin_subprocess(monkeypatch, stdout: bytes):
    monkeypatch.setattr(fv.sys, "platform", "darwin")

    async def fake_exec(*args, **kwargs):
        return _FakeProc(stdout)

    monkeypatch.setattr(fv.asyncio, "create_subprocess_exec", fake_exec)


async def test_check_reports_enabled_when_fdesetup_says_on(monkeypatch):
    _patch_darwin_subprocess(monkeypatch, b"FileVault is On.\n")
    result = await fv.check()
    assert result == {"enabled": True, "platform": "darwin"}


async def test_check_reports_disabled_when_fdesetup_says_off(monkeypatch):
    _patch_darwin_subprocess(monkeypatch, b"FileVault is Off.\n")
    result = await fv.check()
    assert result == {"enabled": False, "platform": "darwin"}


async def test_check_unknown_output_maps_to_none(monkeypatch):
    _patch_darwin_subprocess(monkeypatch, b"something weird\n")
    result = await fv.check()
    assert result["enabled"] is None
    assert result["platform"] == "darwin"
    assert result["error"] == "unknown_output"


async def test_check_on_non_darwin_returns_none(monkeypatch):
    monkeypatch.setattr(fv.sys, "platform", "linux")
    result = await fv.check()
    assert result == {"enabled": None, "platform": "linux"}


async def test_check_handles_missing_fdesetup(monkeypatch):
    monkeypatch.setattr(fv.sys, "platform", "darwin")

    async def boom(*args, **kwargs):
        raise FileNotFoundError("fdesetup not found")

    monkeypatch.setattr(fv.asyncio, "create_subprocess_exec", boom)
    result = await fv.check()
    assert result == {
        "enabled": None,
        "platform": "darwin",
        "error": "fdesetup_missing",
    }


async def test_check_handles_timeout(monkeypatch):
    monkeypatch.setattr(fv.sys, "platform", "darwin")

    class _StuckProc:
        async def communicate(self):
            await asyncio.sleep(10)
            return b"", b""

        def kill(self):
            pass

    async def fake_exec(*args, **kwargs):
        return _StuckProc()

    monkeypatch.setattr(fv.asyncio, "create_subprocess_exec", fake_exec)

    async def fast_wait_for(coro, timeout):
        coro.close()  # avoid "coroutine was never awaited" RuntimeWarning
        raise asyncio.TimeoutError

    monkeypatch.setattr(fv.asyncio, "wait_for", fast_wait_for)

    result = await fv.check()
    assert result["enabled"] is None
    assert result["error"] == "timeout"
