"""Integration tests for the tool-approval gate as wired into a live Arcana
Runtime.

`test_tool_guard.py` covers the gate module in isolation. This file goes one
level up: it builds a real `arcana.Runtime` with the actual `shell` tool,
attaches `tool_guard.confirmation_callback` to the gateway exactly as
`server.py::_get_runtime` does, and exercises the callback path. The
fully-streamed LLM → tool dispatch loop is NOT under test here (no
network); we drive the gateway's confirmation slot directly with a
synthetic `ToolCall`, which is the same surface Arcana hits at runtime.

Also smoke-checks that the legacy substring blacklist in `tools/shell.py`
is gone — the gate is now the only line of defense, and any leftover
short-circuit in the tool body would silently bypass it.
"""

from __future__ import annotations

import pytest

import arcana
from arcana.contracts.tool import ToolCall

import tool_guard
from tool_guard import Mode
from tools.shell import shell


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_guard, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(tool_guard, "ALLOWLIST_PATH", tmp_path / "allowlist.json")
    tool_guard._allowlist_cache["mtime"] = 0.0
    tool_guard._allowlist_cache["entries"] = []
    yield


@pytest.fixture(autouse=True)
def _reset_module_state():
    tool_guard._broadcasters.clear()
    tool_guard._pending.clear()
    yield
    tool_guard._broadcasters.clear()
    for fut in list(tool_guard._pending.values()):
        if not fut.done():
            fut.cancel()
    tool_guard._pending.clear()


@pytest.fixture
def runtime():
    """Build a Runtime mirroring server.py's _get_runtime() shape, then
    attach the approval gate the same way."""
    rt = arcana.Runtime(
        # No real provider keys — we never actually call the LLM here.
        providers={"deepseek": "sk-fake"},
        tools=[shell],
        budget=arcana.Budget(max_cost_usd=0.01),
        config=arcana.RuntimeConfig(default_provider="deepseek"),
    )
    assert rt._tool_gateway is not None, "ToolGateway should be created"
    rt._tool_gateway.confirmation_callback = tool_guard.confirmation_callback
    yield rt


# -----------------------------------------------------------------------------
# Wiring sanity
# -----------------------------------------------------------------------------


def test_shell_tool_declares_requires_confirmation(runtime):
    """If this assertion fires, the shell tool's `@arcana.tool(...)` was
    edited to drop `requires_confirmation=True` and the gate is now
    silently bypassed for the most attack-prone tool. Restore the flag."""
    provider = runtime._tool_gateway.registry.get("shell")
    assert provider is not None, "shell tool not registered"
    assert provider.spec.requires_confirmation is True, (
        "shell tool must declare requires_confirmation=True so Arcana invokes "
        "tool_guard.confirmation_callback before dispatch"
    )


def test_callback_attached_to_gateway(runtime):
    """The gateway's confirmation_callback must be our adapter — not None,
    not some leftover stub."""
    assert runtime._tool_gateway.confirmation_callback is (
        tool_guard.confirmation_callback
    )


# -----------------------------------------------------------------------------
# End-to-end via the real callback path
# -----------------------------------------------------------------------------


async def test_dangerous_shell_blocked_in_confirm_mode(
    runtime, monkeypatch, _isolate_paths
):
    """A dangerous shell call should be rejected when no broadcasters can
    answer — degraded LOG mode allows by design (audit trail), but a real
    REJECT path requires a broadcaster that says no."""
    monkeypatch.setenv("ROBOOT_TOOL_APPROVAL", "confirm")

    async def deny(frame):
        tool_guard.resolve_decision(frame["req_id"], approved=False)

    tool_guard.register_broadcaster(deny)
    call = ToolCall(
        id="t1", name="shell", arguments={"command": "rm -rf /tmp/scratch"}
    )
    spec = runtime._tool_gateway.registry.get("shell").spec
    allowed = await tool_guard.confirmation_callback(call, spec)
    assert allowed is False


async def test_dangerous_shell_allowed_when_user_approves(
    runtime, monkeypatch, _isolate_paths
):
    monkeypatch.setenv("ROBOOT_TOOL_APPROVAL", "confirm")

    async def approve(frame):
        tool_guard.resolve_decision(frame["req_id"], approved=True)

    tool_guard.register_broadcaster(approve)
    call = ToolCall(
        id="t2", name="shell", arguments={"command": "rm -rf /tmp/scratch"}
    )
    spec = runtime._tool_gateway.registry.get("shell").spec
    allowed = await tool_guard.confirmation_callback(call, spec)
    assert allowed is True


async def test_safe_shell_allowed_without_modal(
    runtime, monkeypatch, _isolate_paths
):
    """`git status` should pass straight through — no broadcaster fires,
    no audit record, no modal."""
    monkeypatch.setenv("ROBOOT_TOOL_APPROVAL", "confirm")
    captured: list[dict] = []

    async def bc(frame):
        captured.append(frame)

    tool_guard.register_broadcaster(bc)
    call = ToolCall(id="t3", name="shell", arguments={"command": "git status"})
    spec = runtime._tool_gateway.registry.get("shell").spec
    allowed = await tool_guard.confirmation_callback(call, spec)
    assert allowed is True
    assert captured == [], "safe command should not have triggered the modal"


async def test_off_mode_allows_dangerous(runtime, monkeypatch):
    """When ROBOOT_TOOL_APPROVAL is unset (default off), even dangerous
    commands pass — preserving back-compat for users who haven't opted in.
    The shell tool's body still runs them, but that's expected: opting in
    is the security posture."""
    monkeypatch.delenv("ROBOOT_TOOL_APPROVAL", raising=False)
    assert tool_guard.get_mode() == Mode.OFF
    call = ToolCall(id="t4", name="shell", arguments={"command": "rm -rf /"})
    spec = runtime._tool_gateway.registry.get("shell").spec
    allowed = await tool_guard.confirmation_callback(call, spec)
    assert allowed is True


# -----------------------------------------------------------------------------
# Legacy substring blacklist sanity
# -----------------------------------------------------------------------------


def test_shell_tool_no_legacy_blacklist():
    """The old `tools/shell.py` had an 8-entry substring DANGEROUS_PATTERNS
    list that returned a fake error string before the agent ever reached
    the gate. If anyone re-introduces it, the gate becomes second line of
    defense for those substrings (fine) but the agent gets a misleading
    hardcoded reply for everything else (not fine). Catch the regression."""
    import tools.shell as shell_mod

    assert not hasattr(shell_mod, "DANGEROUS_PATTERNS"), (
        "DANGEROUS_PATTERNS in tools/shell.py was removed in D2; if it's "
        "back, remove it again — danger detection lives in tool_guard.py"
    )


async def test_shell_does_not_self_reject_dangerous_strings(monkeypatch):
    """Calling shell() directly should never return the legacy
    '拒绝执行危险命令' message — that substring blacklist was removed in D2,
    and danger detection now lives entirely in the gate. We invoke a
    harmless `echo` so no real damage if the regression sneaks in."""
    monkeypatch.setenv("ROBOOT_TOOL_APPROVAL", "off")
    # `shell` is the underlying coroutine function (decorator preserves it,
    # storing the spec on `._arcana_tool_spec`).
    result = await shell("echo no-blacklist-test-marker")
    assert "拒绝执行危险命令" not in result
    assert "no-blacklist-test-marker" in result
