"""Bootstrap external MCP servers into Roboot's Arcana runtime.

Reads the `mcp_servers` block of config.yaml, connects each server via Arcana's
MCP client, and registers their tools into the gateway registry. MCP tools
register with dotted `server.tool` names, so they land OUTSIDE tool_guard's
native snapshot (`set_native_tools()`) and gate by default — see the Phase-0
side-effect-first gate. An MCP write tool like `gmail.send_email` therefore
requires approval in CONFIRM mode rather than auto-executing.

Arcana 1.0 ships the low-level `arcana.mcp.setup.setup_mcp_tools(configs,
registry)`, but it connects all servers in one pass and a single failure aborts
the batch. We re-implement the same connect→convert→register loop with
PER-SERVER graceful degrade + a connect timeout, so one slow/missing/broken
server can't hang or crash daemon startup.

Scope: wired into the SERVER (local) event loop only for now. stdio transports
are bound to the loop that spawned the subprocess, so the relay thread (its own
loop) and the Telegram process (separate process) don't yet share these tools —
that's a documented follow-up.
"""

from __future__ import annotations

import logging
from typing import Any

from arcana.contracts.mcp import MCPServerConfig
from arcana.mcp.client import MCPClient
from arcana.mcp.protocol import mcp_tool_to_arcana_spec
from arcana.mcp.tool_provider import MCPToolProvider

logger = logging.getLogger(__name__)


def parse_mcp_configs(config: dict) -> list[MCPServerConfig]:
    """Map the `mcp_servers` block of config.yaml to MCPServerConfig objects.

    Each entry is a mapping accepted by `MCPServerConfig`:
    `{name, transport?, command?, args?, env?, url?, headers?, timeout_ms?,
    capability_prefix?}`. A malformed entry (not a mapping, or missing the
    required `name` / an un-coercible field type) is skipped with a warning —
    a bad MCP config line must never take the daemon down. Unknown extra fields
    are silently ignored by the pydantic model (extra='ignore'), not skipped.
    """
    raw = config.get("mcp_servers")
    if not raw:
        return []
    if not isinstance(raw, list):
        logger.warning("config mcp_servers must be a list, ignoring")
        return []

    configs: list[MCPServerConfig] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict):
            logger.warning("mcp_servers[%d] is not a mapping, skipping", i)
            continue
        try:
            configs.append(MCPServerConfig(**entry))
        except Exception:
            logger.warning(
                "mcp_servers[%d] (%r) invalid, skipping",
                i,
                entry.get("name", "?"),
                exc_info=True,
            )
    return configs


async def connect_mcp_servers(
    registry: Any,
    configs: list[MCPServerConfig],
    *,
    client: MCPClient | None = None,
) -> tuple[MCPClient | None, list[str]]:
    """Connect each MCP server and register its tools into `registry`.

    Per-server graceful degrade: a server that fails to connect (down,
    unreachable, handshake error, internal timeout) is logged and skipped; the
    others still register. The per-tool registration loop is likewise guarded —
    a single bad/hostile tool spec is skipped, never aborting the server or the
    batch. Returns `(client, connected_names)`.

    The caller SHOULD pass a pre-created `client` and publish it for shutdown
    BEFORE awaiting this — so a shutdown that races an in-flight connect can
    still `disconnect_all()` whatever connected so far. One is created if None.
    The caller MUST keep the client alive (its subprocess transports die with
    it) and `await client.disconnect_all()` on shutdown.

    Connect bound: each server's handshake/list_tools is bounded by its
    `MCPServerConfig.timeout_ms` (default 30s); we deliberately do NOT wrap the
    connect in an external `asyncio.wait_for`, because cancelling Arcana's
    `MCPClient.connect()` mid-handshake orphans the spawned subprocess —
    `connect()` registers the connection (so `disconnect_all()` can reach it)
    only after a fully successful handshake, and has no try/finally cleanup on
    failure. A server that spawns its subprocess then fails the handshake may
    therefore still leak that subprocess; we log it so it's observable. The real
    fix is upstream in Arcana (wrap `connect()` in try/finally). Roboot can't
    reach the half-open transport through the public client API.
    """
    if not configs:
        return client, []
    if client is None:
        client = MCPClient()

    connected: list[str] = []
    for config in configs:
        try:
            mcp_tools = await client.connect(config)
        except Exception:
            logger.warning(
                "MCP server %r failed to connect; skipping its tools "
                "(a spawned subprocess may be orphaned — Arcana connect() has "
                "no failure cleanup)",
                config.name,
                exc_info=True,
            )
            # No-op if the connection never registered (the common failure
            # case); cleans a fully-registered one if we somehow got here.
            try:
                await client.disconnect(config.name)
            except Exception:
                pass
            continue

        registered = 0
        for mcp_tool in mcp_tools:
            try:
                spec = mcp_tool_to_arcana_spec(
                    mcp_tool,
                    server_name=config.name,
                    capability_prefix=config.capability_prefix,
                )
            except Exception:
                logger.warning(
                    "MCP tool %r.%r: spec conversion failed, skipping",
                    config.name,
                    getattr(mcp_tool, "name", "?"),
                    exc_info=True,
                )
                continue
            # Don't silently clobber an existing tool (a duplicate server name,
            # or a tool already registered) — a same-named overwrite of a gated
            # tool is a quiet failure mode.
            if registry.get(spec.name) is not None:
                logger.warning(
                    "MCP tool %r already registered; skipping (name collision)",
                    spec.name,
                )
                continue
            try:
                registry.register(
                    MCPToolProvider(
                        client=client,
                        server_name=config.name,
                        mcp_tool_name=mcp_tool.name,
                        arcana_spec=spec,
                    )
                )
                registered += 1
            except Exception:
                logger.warning(
                    "MCP tool %r: registration failed, skipping",
                    spec.name,
                    exc_info=True,
                )

        connected.append(config.name)
        logger.info(
            "MCP server %r connected: %d/%d tool(s) registered "
            "(external → gated as writes by tool_guard)",
            config.name,
            registered,
            len(mcp_tools),
        )
    return client, connected
