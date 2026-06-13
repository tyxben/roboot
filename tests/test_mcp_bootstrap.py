"""Tests for mcp_bootstrap — config parsing + graceful MCP connect/register.

No real MCP server is spawned: `MCPClient` is monkeypatched with a fake whose
per-server behavior (return tools / raise / hang) is keyed by config name. The
registry, MCPToolProvider, MCPToolSpec, and side-effect inference are the real
Arcana objects, so registration + dotted naming + WRITE classification are
exercised end-to-end.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

import mcp_bootstrap
from arcana.contracts.mcp import MCPServerConfig, MCPToolSpec
from arcana.tool_gateway.registry import ToolRegistry


# -----------------------------------------------------------------------------
# parse_mcp_configs
# -----------------------------------------------------------------------------


def test_parse_missing_or_empty_returns_empty():
    assert mcp_bootstrap.parse_mcp_configs({}) == []
    assert mcp_bootstrap.parse_mcp_configs({"mcp_servers": None}) == []
    assert mcp_bootstrap.parse_mcp_configs({"mcp_servers": []}) == []


def test_parse_non_list_ignored(caplog):
    with caplog.at_level(logging.WARNING, logger=mcp_bootstrap.logger.name):
        assert mcp_bootstrap.parse_mcp_configs({"mcp_servers": {"name": "x"}}) == []
    assert any("must be a list" in r.message for r in caplog.records)


def test_parse_valid_entry_maps_all_fields():
    cfg = {
        "mcp_servers": [
            {
                "name": "messageinfra",
                "transport": "stdio",
                "command": "/anaconda/bin/python",
                "args": ["-m", "messageinfra.mcp_server"],
                "env": {"MESSAGEINFRA_URL": "http://localhost:9990"},
            }
        ]
    }
    out = mcp_bootstrap.parse_mcp_configs(cfg)
    assert len(out) == 1
    c = out[0]
    assert isinstance(c, MCPServerConfig)
    assert c.name == "messageinfra"
    assert c.command == "/anaconda/bin/python"
    assert c.args == ["-m", "messageinfra.mcp_server"]
    assert c.env["MESSAGEINFRA_URL"] == "http://localhost:9990"
    assert c.transport.value == "stdio"


def test_parse_skips_malformed_entries(caplog):
    cfg = {
        "mcp_servers": [
            "not-a-dict",
            {"transport": "stdio"},  # missing required 'name'
            {"name": "ok", "command": "x"},  # valid
        ]
    }
    with caplog.at_level(logging.WARNING, logger=mcp_bootstrap.logger.name):
        out = mcp_bootstrap.parse_mcp_configs(cfg)
    assert [c.name for c in out] == ["ok"]  # bad entries skipped, not fatal


# -----------------------------------------------------------------------------
# connect_mcp_servers (fake MCPClient)
# -----------------------------------------------------------------------------


class _FakeClient:
    """Stand-in for arcana MCPClient. `behavior[name]` is a list[MCPToolSpec]
    to return, an Exception to raise, or the string 'hang' to sleep forever."""

    behavior: dict = {}
    last: "_FakeClient | None" = None

    def __init__(self):
        self.disconnected: list[str] = []
        self.disconnect_all_called = False
        _FakeClient.last = self

    async def connect(self, config):
        beh = _FakeClient.behavior.get(config.name, [])
        if isinstance(beh, Exception):
            raise beh
        return beh

    async def disconnect(self, name):
        self.disconnected.append(name)

    async def disconnect_all(self):
        self.disconnect_all_called = True


@pytest.fixture
def fake_client(monkeypatch):
    _FakeClient.behavior = {}
    _FakeClient.last = None
    monkeypatch.setattr(mcp_bootstrap, "MCPClient", _FakeClient)
    return _FakeClient


def _tool(name: str) -> MCPToolSpec:
    return MCPToolSpec(name=name, description="x", input_schema={"type": "object"})


async def test_connect_empty_returns_none():
    client, connected = await mcp_bootstrap.connect_mcp_servers(ToolRegistry(), [])
    assert client is None
    assert connected == []


async def test_connect_success_registers_dotted_tools(fake_client):
    fake_client.behavior = {"mi": [_tool("get_briefing"), _tool("send_alert")]}
    reg = ToolRegistry()
    client, connected = await mcp_bootstrap.connect_mcp_servers(
        reg, [MCPServerConfig(name="mi", command="x")]
    )
    assert connected == ["mi"]
    names = set(reg.list_tools())
    # Dotted server.tool naming → these land OUTSIDE tool_guard's native set.
    assert "mi.get_briefing" in names
    assert "mi.send_alert" in names
    # Arcana classifies 'send_*' WRITE / 'get_*' READ; the WRITE one gates.
    assert reg.get("mi.send_alert").spec.side_effect.value == "write"
    assert reg.get("mi.get_briefing").spec.side_effect.value == "read"


async def test_connect_graceful_degrade(fake_client, caplog):
    fake_client.behavior = {
        "down": RuntimeError("connection refused"),
        "up": [_tool("get_x")],
    }
    reg = ToolRegistry()
    cfgs = [
        MCPServerConfig(name="down", command="x"),
        MCPServerConfig(name="up", command="x"),
    ]
    with caplog.at_level(logging.WARNING, logger=mcp_bootstrap.logger.name):
        client, connected = await mcp_bootstrap.connect_mcp_servers(reg, cfgs)
    # The down server is skipped; the up server still registers (no crash).
    assert connected == ["up"]
    assert "up.get_x" in set(reg.list_tools())
    assert "down" in fake_client.last.disconnected  # half-open cleanup attempted
    assert any("down" in r.message for r in caplog.records)


async def test_connect_failure_skips_server(fake_client):
    """A server whose connect raises (down / internal handshake timeout) is
    skipped; siblings still register. We deliberately don't use an external
    wait_for (cancelling connect orphans the subprocess), so 'timeout' arrives
    as an exception from the bounded handshake."""
    fake_client.behavior = {
        "slow": asyncio.TimeoutError("handshake timed out"),
        "ok": [_tool("get_x")],
    }
    reg = ToolRegistry()
    cfgs = [
        MCPServerConfig(name="slow", command="x"),
        MCPServerConfig(name="ok", command="x"),
    ]
    client, connected = await mcp_bootstrap.connect_mcp_servers(reg, cfgs)
    assert connected == ["ok"]
    assert "ok.get_x" in set(reg.list_tools())


async def test_connect_skips_colliding_tool(fake_client, caplog):
    """A tool whose dotted name already exists (e.g. duplicate server name) is
    skipped, not silently overwritten."""
    fake_client.behavior = {"mi": [_tool("get_x")]}
    reg = ToolRegistry()
    cfgs = [
        MCPServerConfig(name="mi", command="a"),
        MCPServerConfig(name="mi", command="b"),  # same dotted names
    ]
    with caplog.at_level(logging.WARNING, logger=mcp_bootstrap.logger.name):
        client, connected = await mcp_bootstrap.connect_mcp_servers(reg, cfgs)
    assert connected == ["mi", "mi"]
    assert sorted(reg.list_tools()) == ["mi.get_x"]  # registered once
    assert any("collision" in r.message for r in caplog.records)


async def test_connect_skips_bad_tool_spec(fake_client, monkeypatch, caplog):
    """A tool whose spec conversion raises (hostile/buggy server) is skipped;
    sibling tools and later servers are unaffected — no client leak, no abort."""
    fake_client.behavior = {"mi": [_tool("bad"), _tool("good")]}
    real = mcp_bootstrap.mcp_tool_to_arcana_spec

    def flaky(mcp_tool, **kw):
        if mcp_tool.name == "bad":
            raise ValueError("hostile schema")
        return real(mcp_tool, **kw)

    monkeypatch.setattr(mcp_bootstrap, "mcp_tool_to_arcana_spec", flaky)
    reg = ToolRegistry()
    with caplog.at_level(logging.WARNING, logger=mcp_bootstrap.logger.name):
        client, connected = await mcp_bootstrap.connect_mcp_servers(
            reg, [MCPServerConfig(name="mi", command="x")]
        )
    assert connected == ["mi"]
    assert set(reg.list_tools()) == {"mi.good"}  # bad skipped, good kept
    assert any("spec conversion failed" in r.message for r in caplog.records)


async def test_connect_uses_and_returns_passed_client(fake_client):
    """A pre-created client (published before connect for shutdown safety) is
    used and returned, not replaced."""
    fake_client.behavior = {"mi": [_tool("get_x")]}
    pre = mcp_bootstrap.MCPClient()  # the fake, via fixture monkeypatch
    client, connected = await mcp_bootstrap.connect_mcp_servers(
        ToolRegistry(), [MCPServerConfig(name="mi", command="x")], client=pre
    )
    assert client is pre


async def test_connect_returns_live_client_for_shutdown(fake_client):
    fake_client.behavior = {"mi": [_tool("get_x")]}
    client, connected = await mcp_bootstrap.connect_mcp_servers(
        ToolRegistry(), [MCPServerConfig(name="mi", command="x")]
    )
    assert client is not None
    await client.disconnect_all()
    assert client.disconnect_all_called is True
