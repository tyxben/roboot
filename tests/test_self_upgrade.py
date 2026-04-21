"""Tests for self_upgrade.run_upgrade_loop / _tick.

Strategy: monkeypatch ``self_upgrade._run`` (the single subprocess entry
point) with a scripted responder that returns a pre-defined result for
each argv the code under test invokes. That's less fragile than stubbing
``asyncio.create_subprocess_exec`` directly while still proving the
exact git commands fire in the right order.

``restart_daemon`` is always patched — we never want tests to actually
re-exec the pytest process.
"""

from __future__ import annotations

from typing import Callable, List, Optional

import pytest

import self_upgrade


class _Result:
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _install_run_script(
    monkeypatch,
    responder: Callable[[tuple, dict], _Result],
) -> List[tuple]:
    """Install a scripted _run. Returns a list that captures every call's argv."""
    calls: List[tuple] = []

    async def fake_run(*argv, cwd=None, timeout=self_upgrade.GIT_TIMEOUT):
        calls.append(tuple(argv))
        return responder(tuple(argv), {"cwd": cwd, "timeout": timeout})

    monkeypatch.setattr(self_upgrade, "_run", fake_run)
    return calls


@pytest.fixture(autouse=True)
def _patch_restart(monkeypatch):
    """Never actually re-exec the interpreter during tests."""
    restart_calls: List[Optional[str]] = []

    def fake_restart(old_sha=None):
        restart_calls.append(old_sha)

    monkeypatch.setattr(self_upgrade, "restart_daemon", fake_restart)
    return restart_calls


@pytest.fixture(autouse=True)
def _isolate_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(self_upgrade, "SENTINEL_PATH", tmp_path / ".upgrade_pending")
    return tmp_path / ".upgrade_pending"


@pytest.fixture
def _fake_server(monkeypatch):
    """Install a fake ``server`` module with get_in_flight_count."""
    import sys
    import types

    mod = types.ModuleType("server")
    mod._in_flight = 0
    mod._active_ws_clients = set()
    mod._relay_client = None
    mod._relay_broadcast = None

    def get_in_flight_count():
        return mod._in_flight

    mod.get_in_flight_count = get_in_flight_count
    monkeypatch.setitem(sys.modules, "server", mod)
    return mod


async def test_no_new_commits_path(monkeypatch, _patch_restart, _fake_server):
    def responder(argv, _kw):
        if argv[:2] == ("git", "rev-parse"):
            return _Result(0, "aaaa111\n")
        if argv[:2] == ("git", "fetch"):
            return _Result(0)
        if argv[:3] == ("git", "rev-list", "HEAD..origin/main"):
            return _Result(0, "0\n")
        raise AssertionError(f"unexpected call: {argv}")

    calls = _install_run_script(monkeypatch, responder)

    await self_upgrade._tick(app=None)

    # Should stop after rev-list returns 0.
    cmds = [c[:3] for c in calls]
    assert ("git", "rev-parse", "HEAD") in cmds
    assert ("git", "fetch", "origin") in cmds
    assert ("git", "rev-list", "HEAD..origin/main") in cmds
    # Must NOT have touched status, pull, pytest, or restart.
    assert not any(c[:2] == ("git", "status") for c in calls)
    assert not any(c[:2] == ("git", "pull") for c in calls)
    assert not any("pytest" in " ".join(c) for c in calls)
    assert _patch_restart == []


async def test_dirty_tree_skips(monkeypatch, _patch_restart, _fake_server):
    def responder(argv, _kw):
        if argv[:2] == ("git", "rev-parse"):
            return _Result(0, "aaaa111\n")
        if argv[:2] == ("git", "fetch"):
            return _Result(0)
        if argv[:3] == ("git", "rev-list", "HEAD..origin/main"):
            return _Result(0, "2\n")
        if argv[:3] == ("git", "status", "--porcelain"):
            return _Result(0, " M server.py\n")
        raise AssertionError(f"unexpected call: {argv}")

    calls = _install_run_script(monkeypatch, responder)

    await self_upgrade._tick(app=None)

    # Stopped at status; must not pull/test/restart.
    assert any(c[:3] == ("git", "status", "--porcelain") for c in calls)
    assert not any(c[:2] == ("git", "pull") for c in calls)
    assert not any("pytest" in " ".join(c) for c in calls)
    assert _patch_restart == []


async def test_in_flight_defers(monkeypatch, _patch_restart, _fake_server):
    _fake_server._in_flight = 1  # a chat turn is in progress

    def responder(argv, _kw):
        if argv[:2] == ("git", "rev-parse"):
            return _Result(0, "aaaa111\n")
        if argv[:2] == ("git", "fetch"):
            return _Result(0)
        if argv[:3] == ("git", "rev-list", "HEAD..origin/main"):
            return _Result(0, "1\n")
        if argv[:3] == ("git", "status", "--porcelain"):
            return _Result(0, "")
        raise AssertionError(f"unexpected call: {argv}")

    calls = _install_run_script(monkeypatch, responder)

    await self_upgrade._tick(app=None)

    # Deferred after status check — must not have pulled.
    assert not any(c[:2] == ("git", "pull") for c in calls)
    assert not any("pytest" in " ".join(c) for c in calls)
    assert _patch_restart == []


async def test_test_failure_rolls_back(
    monkeypatch, _patch_restart, _fake_server, _isolate_sentinel
):
    head_sequence = iter(
        [
            "oldsha0000000\n",  # _current_head before fetch
            "newsha1111111\n",  # _current_head after pull
        ]
    )
    reset_calls: List[tuple] = []

    def responder(argv, _kw):
        if argv[:2] == ("git", "rev-parse"):
            return _Result(0, next(head_sequence))
        if argv[:2] == ("git", "fetch"):
            return _Result(0)
        if argv[:3] == ("git", "rev-list", "HEAD..origin/main"):
            return _Result(0, "3\n")
        if argv[:3] == ("git", "status", "--porcelain"):
            return _Result(0, "")
        if argv[:4] == ("git", "pull", "--ff-only", "origin"):
            return _Result(0)
        # smoke test: sys.executable -m pytest ...
        if "pytest" in " ".join(argv):
            return _Result(1, "", "2 failed")
        if argv[:3] == ("git", "reset", "--hard"):
            reset_calls.append(argv)
            return _Result(0)
        raise AssertionError(f"unexpected call: {argv}")

    _install_run_script(monkeypatch, responder)

    await self_upgrade._tick(app=None)

    # git reset --hard <old_sha> must have been called, and restart not triggered.
    assert len(reset_calls) == 1
    assert reset_calls[0][:3] == ("git", "reset", "--hard")
    assert reset_calls[0][3] == "oldsha0000000"
    assert _patch_restart == []
    # Sentinel not written on failure.
    assert not _isolate_sentinel.exists()


async def test_success_writes_sentinel_and_restarts(
    monkeypatch, _patch_restart, _fake_server, _isolate_sentinel
):
    head_sequence = iter(
        [
            "oldsha0000000\n",
            "newsha1111111\n",
        ]
    )

    def responder(argv, _kw):
        if argv[:2] == ("git", "rev-parse"):
            return _Result(0, next(head_sequence))
        if argv[:2] == ("git", "fetch"):
            return _Result(0)
        if argv[:3] == ("git", "rev-list", "HEAD..origin/main"):
            return _Result(0, "1\n")
        if argv[:3] == ("git", "status", "--porcelain"):
            return _Result(0, "")
        if argv[:4] == ("git", "pull", "--ff-only", "origin"):
            return _Result(0)
        if "pytest" in " ".join(argv):
            return _Result(0, "all passed", "")
        if argv[:3] == ("git", "reset", "--hard"):
            raise AssertionError("should not roll back on success")
        raise AssertionError(f"unexpected call: {argv}")

    _install_run_script(monkeypatch, responder)

    await self_upgrade._tick(app=None)

    assert _isolate_sentinel.exists()
    body = _isolate_sentinel.read_text()
    assert "newsha1111111" in body
    # restart_daemon called once with old_sha.
    assert _patch_restart == ["oldsha0000000"]


async def test_fetch_failure_skips_safely(
    monkeypatch, _patch_restart, _fake_server
):
    def responder(argv, _kw):
        if argv[:2] == ("git", "rev-parse"):
            return _Result(0, "aaaa\n")
        if argv[:2] == ("git", "fetch"):
            return _Result(1, "", "network down")
        raise AssertionError(f"unexpected call: {argv}")

    calls = _install_run_script(monkeypatch, responder)
    await self_upgrade._tick(app=None)

    # Nothing beyond fetch should have fired.
    assert not any(c[:2] == ("git", "rev-list") for c in calls)
    assert _patch_restart == []
