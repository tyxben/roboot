"""Tests for the ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG gate in self_upgrade.

We script ``self_upgrade._run`` (the subprocess entry point) and
monkeypatch ``_find_verified_tag_at`` directly when we want to simulate
tag-verification outcomes without also dictating the exact argv for
``git tag`` / ``git verify-tag``.

``restart_daemon`` is always patched so tests never re-exec the
interpreter.
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


def _install_run_script(monkeypatch, responder: Callable[[tuple, dict], _Result]) -> List[tuple]:
    calls: List[tuple] = []

    async def fake_run(*argv, cwd=None, timeout=self_upgrade.GIT_TIMEOUT):
        calls.append(tuple(argv))
        return responder(tuple(argv), {"cwd": cwd, "timeout": timeout})

    monkeypatch.setattr(self_upgrade, "_run", fake_run)
    return calls


@pytest.fixture(autouse=True)
def _patch_restart(monkeypatch):
    restart_calls: List[Optional[str]] = []

    def fake_restart(old_sha=None):
        restart_calls.append(old_sha)

    monkeypatch.setattr(self_upgrade, "restart_daemon", fake_restart)
    return restart_calls


@pytest.fixture(autouse=True)
def _isolate_sentinel(tmp_path, monkeypatch):
    monkeypatch.setattr(
        self_upgrade, "SENTINEL_PATH", tmp_path / ".upgrade_pending"
    )
    return tmp_path / ".upgrade_pending"


@pytest.fixture
def _fake_server(monkeypatch):
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


def _baseline_responder(*, new_remote_head: str = "newsha1111111"):
    """Return a responder that walks the happy path up to (but not
    including) the signed-tag gate, and lets the rest of the flow succeed
    if the gate lets it proceed."""
    head_sequence = iter(["oldsha0000000\n", "newsha_after_pull\n"])

    def responder(argv, _kw):
        if argv[:3] == ("git", "rev-parse", "HEAD"):
            return _Result(0, next(head_sequence))
        if argv[:3] == ("git", "rev-parse", "origin/main"):
            return _Result(0, f"{new_remote_head}\n")
        if argv[:2] == ("git", "fetch"):
            return _Result(0)
        if argv[:3] == ("git", "rev-list", "HEAD..origin/main"):
            return _Result(0, "1\n")
        if argv[:3] == ("git", "status", "--porcelain"):
            return _Result(0, "")
        if argv[:4] == ("git", "pull", "--ff-only", "origin"):
            return _Result(0)
        if "pytest" in " ".join(argv):
            return _Result(0, "ok", "")
        if argv[:3] == ("git", "reset", "--hard"):
            raise AssertionError("should not roll back on success")
        raise AssertionError(f"unexpected call: {argv}")

    return responder


async def test_gate_off_bypasses_tag_check(
    monkeypatch, _patch_restart, _fake_server, _isolate_sentinel
):
    """When the env var is unset, the gate never runs: even with no tag
    present, the upgrade proceeds to pull + smoke-test + restart."""
    monkeypatch.delenv("ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG", raising=False)

    tag_lookup_calls: List[str] = []

    async def fake_find_tag(sha):
        tag_lookup_calls.append(sha)
        return None

    monkeypatch.setattr(self_upgrade, "_find_verified_tag_at", fake_find_tag)

    calls = _install_run_script(monkeypatch, _baseline_responder())

    await self_upgrade._tick(app=None)

    # Gate skipped entirely.
    assert tag_lookup_calls == []
    # Pull + pytest ran; restart fired.
    assert any(c[:4] == ("git", "pull", "--ff-only", "origin") for c in calls)
    assert any("pytest" in " ".join(c) for c in calls)
    assert _patch_restart == ["oldsha0000000"]


async def test_gate_on_no_tag_skips(
    monkeypatch, _patch_restart, _fake_server, _isolate_sentinel
):
    """Env var set, no verified tag at origin/main → skip this tick:
    must not pull, must not test, must not restart."""
    monkeypatch.setenv("ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG", "1")

    async def fake_find_tag(sha):
        assert sha == "newsha1111111"
        return None

    monkeypatch.setattr(self_upgrade, "_find_verified_tag_at", fake_find_tag)

    calls = _install_run_script(monkeypatch, _baseline_responder())

    await self_upgrade._tick(app=None)

    assert not any(c[:2] == ("git", "pull") for c in calls)
    assert not any("pytest" in " ".join(c) for c in calls)
    assert _patch_restart == []
    assert not _isolate_sentinel.exists()


async def test_gate_on_verified_tag_pulls(
    monkeypatch, _patch_restart, _fake_server, _isolate_sentinel
):
    """Env var set, a verified signed tag points at origin/main → pull
    proceeds through smoke-test and restart."""
    monkeypatch.setenv("ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG", "1")

    lookups: List[str] = []

    async def fake_find_tag(sha):
        lookups.append(sha)
        return "v0.3.0"

    monkeypatch.setattr(self_upgrade, "_find_verified_tag_at", fake_find_tag)

    calls = _install_run_script(monkeypatch, _baseline_responder())

    await self_upgrade._tick(app=None)

    assert lookups == ["newsha1111111"]
    assert any(c[:4] == ("git", "pull", "--ff-only", "origin") for c in calls)
    assert any("pytest" in " ".join(c) for c in calls)
    assert _patch_restart == ["oldsha0000000"]
    assert _isolate_sentinel.exists()


async def test_gate_on_failing_verification_skips(
    monkeypatch, _patch_restart, _fake_server, _isolate_sentinel
):
    """Env var set, a tag exists at origin/main but verify-tag fails →
    helper returns None → upgrade is skipped."""
    monkeypatch.setenv("ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG", "1")

    # Use the real helper here, but script git tag + git verify-tag via _run:
    # - "git tag --points-at" returns "v0.3.0"
    # - "git verify-tag v0.3.0" returns non-zero (bad signature)
    def responder(argv, _kw):
        if argv[:3] == ("git", "rev-parse", "HEAD"):
            return _Result(0, "oldsha0000000\n")
        if argv[:3] == ("git", "rev-parse", "origin/main"):
            return _Result(0, "newsha1111111\n")
        if argv[:2] == ("git", "fetch"):
            return _Result(0)
        if argv[:3] == ("git", "rev-list", "HEAD..origin/main"):
            return _Result(0, "1\n")
        if argv[:3] == ("git", "status", "--porcelain"):
            return _Result(0, "")
        if argv[:3] == ("git", "tag", "--points-at"):
            return _Result(0, "v0.3.0\n")
        if argv[:2] == ("git", "verify-tag"):
            return _Result(1, "", "gpg: BAD signature")
        if argv[:2] == ("git", "pull"):
            raise AssertionError("should not pull when verification fails")
        if "pytest" in " ".join(argv):
            raise AssertionError("should not test when verification fails")
        raise AssertionError(f"unexpected call: {argv}")

    calls = _install_run_script(monkeypatch, responder)

    await self_upgrade._tick(app=None)

    # Gate actually ran.
    assert any(c[:3] == ("git", "tag", "--points-at") for c in calls)
    assert any(c[:2] == ("git", "verify-tag") for c in calls)
    # But it refused to proceed.
    assert not any(c[:2] == ("git", "pull") for c in calls)
    assert not any("pytest" in " ".join(c) for c in calls)
    assert _patch_restart == []
    assert not _isolate_sentinel.exists()


async def test_find_verified_tag_at_returns_tag_on_success(monkeypatch):
    """Unit-test the helper itself: picks the first tag whose verify-tag
    returns 0 and matches v* pattern."""

    def responder(argv, _kw):
        if argv[:3] == ("git", "tag", "--points-at"):
            # Mix of release-style and non-release tags; verify-tag will
            # be called only for release-style ones.
            return _Result(0, "not-a-release\nv0.3.0\n")
        if argv[:2] == ("git", "verify-tag"):
            if argv[2] == "v0.3.0":
                return _Result(0, "", "Good signature")
            raise AssertionError(f"unexpected verify-tag for {argv[2]}")
        raise AssertionError(f"unexpected call: {argv}")

    _install_run_script(monkeypatch, responder)

    tag = await self_upgrade._find_verified_tag_at("deadbeefdeadbeef")
    assert tag == "v0.3.0"


async def test_find_verified_tag_at_returns_none_when_no_tags(monkeypatch):
    def responder(argv, _kw):
        if argv[:3] == ("git", "tag", "--points-at"):
            return _Result(0, "\n")
        raise AssertionError(f"unexpected call: {argv}")

    _install_run_script(monkeypatch, responder)

    tag = await self_upgrade._find_verified_tag_at("deadbeefdeadbeef")
    assert tag is None
