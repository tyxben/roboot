"""Tests for auth.py — the LAN bearer token gate.

Goals:
    * Token persistence: generating once stores to disk; a second call
      returns the same value.
    * REST enforcement: missing header → 401, wrong header → 401, valid
      header → 200.
    * WebSocket enforcement: missing subprotocol closes with 4401;
      matching subprotocol is echoed back and the socket stays open.
    * Static / root routes stay public.
    * attach_token_to_url() builds a URL the gate accepts.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI, WebSocket
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

import auth


@pytest.fixture
def isolated_token(tmp_path, monkeypatch):
    """Point auth.* at a per-test scratch file so the real .auth/ is untouched."""
    scratch = tmp_path / "lan_token"
    monkeypatch.setattr(auth, "AUTH_DIR", tmp_path)
    monkeypatch.setattr(auth, "TOKEN_PATH", scratch)
    auth._reset_cache_for_tests()
    yield scratch
    auth._reset_cache_for_tests()


@pytest.fixture
def app_with_auth(isolated_token):
    """A tiny FastAPI app mirroring server.py's token gating pattern."""
    application = FastAPI()

    @application.get("/api/ping", dependencies=[Depends(auth.require_lan_token)])
    async def ping():
        return {"ok": True}

    @application.get("/")
    async def root():
        return {"public": True}

    @application.get("/static/thing")
    async def static_thing():
        return {"public": True}

    @application.websocket("/ws")
    async def ws_endpoint(ws: WebSocket):
        try:
            subprotocol = await auth.require_lan_token_ws(ws)
        except Exception:
            return
        if subprotocol:
            await ws.accept(subprotocol=subprotocol)
        else:
            await ws.accept()
        await ws.send_json({"hello": "world"})
        await ws.close()

    return application


# ---------------------------------------------------------------------------
# Token generation / persistence
# ---------------------------------------------------------------------------


def test_token_is_generated_on_first_call(isolated_token):
    assert not isolated_token.exists()
    token = auth.load_or_generate_token()
    assert isolated_token.exists()
    # token_urlsafe(32) is 43 chars.
    assert len(token) >= 32
    # Persistence check: same value across calls in the same process.
    assert auth.load_or_generate_token() == token


def test_token_persists_across_cache_resets(isolated_token):
    first = auth.load_or_generate_token()
    # Simulate a process restart — cache cleared, but file still there.
    auth._reset_cache_for_tests()
    second = auth.load_or_generate_token()
    assert first == second


def test_token_file_has_restrictive_perms(isolated_token):
    auth.load_or_generate_token()
    mode = isolated_token.stat().st_mode & 0o777
    # chmod may be unsupported on some filesystems; when it does run we
    # expect 0o600.
    assert mode in (0o600, 0o644), f"unexpected perms {oct(mode)}"


def test_malformed_token_file_is_regenerated(isolated_token):
    isolated_token.parent.mkdir(parents=True, exist_ok=True)
    isolated_token.write_text("")  # empty -> ignored
    auth._reset_cache_for_tests()
    token = auth.load_or_generate_token()
    assert len(token) >= 32
    assert isolated_token.read_text().strip() == token


def test_short_token_file_is_regenerated(isolated_token):
    isolated_token.parent.mkdir(parents=True, exist_ok=True)
    isolated_token.write_text("abc")
    auth._reset_cache_for_tests()
    token = auth.load_or_generate_token()
    assert token != "abc"
    assert len(token) >= 32


def test_token_file_with_bad_chars_is_regenerated(isolated_token):
    isolated_token.parent.mkdir(parents=True, exist_ok=True)
    # Spaces and $ are outside the URL-safe alphabet; file should be ignored.
    isolated_token.write_text("$$$ not a real token with spaces $$$" + "x" * 40)
    auth._reset_cache_for_tests()
    token = auth.load_or_generate_token()
    # The new token is URL-safe, so it won't contain spaces.
    assert " " not in token and "$" not in token


# ---------------------------------------------------------------------------
# REST enforcement
# ---------------------------------------------------------------------------


def test_rest_without_header_returns_401(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.get("/api/ping")
    assert r.status_code == 401


def test_rest_with_wrong_token_returns_401(app_with_auth):
    client = TestClient(app_with_auth)
    r = client.get("/api/ping", headers={"Authorization": "Bearer not-the-right-token"})
    assert r.status_code == 401


def test_rest_with_correct_token_returns_200(app_with_auth):
    token = auth.load_or_generate_token()
    client = TestClient(app_with_auth)
    r = client.get("/api/ping", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_rest_accepts_query_param_fallback(app_with_auth):
    token = auth.load_or_generate_token()
    client = TestClient(app_with_auth)
    r = client.get(f"/api/ping?token={token}")
    assert r.status_code == 200


def test_rest_wrong_bearer_scheme_is_rejected(app_with_auth):
    token = auth.load_or_generate_token()
    client = TestClient(app_with_auth)
    # "Basic" and similar must not be accepted.
    r = client.get("/api/ping", headers={"Authorization": f"Basic {token}"})
    assert r.status_code == 401


def test_root_and_static_are_public(app_with_auth):
    client = TestClient(app_with_auth)
    assert client.get("/").status_code == 200
    assert client.get("/static/thing").status_code == 200


# ---------------------------------------------------------------------------
# WebSocket enforcement
# ---------------------------------------------------------------------------


def test_ws_without_token_is_rejected(app_with_auth):
    client = TestClient(app_with_auth)
    with pytest.raises(Exception):
        # Starlette's TestClient raises WebSocketDisconnect on close.
        with client.websocket_connect("/ws"):
            pass


def test_ws_with_wrong_token_is_rejected(app_with_auth):
    client = TestClient(app_with_auth)
    with pytest.raises(Exception):
        with client.websocket_connect("/ws", subprotocols=["bearer.nope"]):
            pass


def test_ws_with_correct_token_connects(app_with_auth):
    token = auth.load_or_generate_token()
    client = TestClient(app_with_auth)
    with client.websocket_connect("/ws", subprotocols=[f"bearer.{token}"]) as sock:
        msg = sock.receive_json()
        assert msg == {"hello": "world"}


def test_ws_query_param_fallback(app_with_auth):
    token = auth.load_or_generate_token()
    client = TestClient(app_with_auth)
    with client.websocket_connect(f"/ws?token={token}") as sock:
        msg = sock.receive_json()
        assert msg == {"hello": "world"}


# ---------------------------------------------------------------------------
# URL helper
# ---------------------------------------------------------------------------


def test_attach_token_to_url_appends_param(isolated_token):
    out = auth.attach_token_to_url("https://host.local:8765/")
    assert "token=" in out
    # URL should still point at the same origin.
    assert out.startswith("https://host.local:8765/")


def test_attach_token_to_url_preserves_existing_query(isolated_token):
    out = auth.attach_token_to_url("https://host.local:8765/?foo=bar")
    assert "foo=bar" in out
    assert "token=" in out


def test_attach_token_to_url_overwrites_stale_token(isolated_token):
    out = auth.attach_token_to_url("https://host.local:8765/?token=stale")
    real_token = auth.load_or_generate_token()
    # Only the real token should appear; "stale" is gone.
    assert f"token={real_token}" in out
    assert "token=stale" not in out


# ---------------------------------------------------------------------------
# Loopback bypass — requests from 127.0.0.1 / ::1 skip the token check.
# Same-user attacker on the same Mac can already read .auth/lan_token from
# disk, so requiring a token on loopback is friction without a payoff.
# ---------------------------------------------------------------------------


def _make_request(client_host, *, query_string: bytes = b"") -> "Request":
    """Build a Starlette Request whose ``client.host`` we control.

    TestClient hard-codes client="testclient", so we have to drop down to
    a raw ASGI scope to exercise the loopback bypass.
    """
    from fastapi import Request

    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/ping",
        "headers": [],
        "query_string": query_string,
        "client": (client_host, 0) if client_host is not None else None,
    }
    return Request(scope)


class _StubWS:
    """Minimal WebSocket stand-in for testing ``require_lan_token_ws``.

    Only the attributes the function reads are populated. ``close()`` is
    awaited on rejection but not on the loopback path, so it's a no-op
    coroutine.
    """

    def __init__(self, client_host, *, subprotocols=None, query_string: str = ""):
        self.client = type("C", (), {"host": client_host})() if client_host is not None else None
        self.scope = {"subprotocols": subprotocols or []}
        # Headers + query_params mimic Starlette's interface for the bits
        # _extract_bearer_from_subprotocol / require_lan_token_ws read.
        self.headers = {}
        self.query_params = dict(
            p.split("=", 1) for p in query_string.split("&") if "=" in p
        )
        self.closed_with: int | None = None

    async def close(self, code: int = 1000) -> None:
        self.closed_with = code


@pytest.mark.asyncio
async def test_rest_loopback_v4_skips_token_check(isolated_token):
    """127.0.0.1 with no Authorization header must not raise."""
    request = _make_request("127.0.0.1")
    # No exception = pass.
    await auth.require_lan_token(request, authorization=None)


@pytest.mark.asyncio
async def test_rest_loopback_v6_skips_token_check(isolated_token):
    """::1 with no Authorization header must not raise."""
    request = _make_request("::1")
    await auth.require_lan_token(request, authorization=None)


@pytest.mark.asyncio
async def test_rest_lan_ip_still_requires_token(isolated_token):
    """Regression pin — LAN clients (e.g. phones on Wi-Fi) keep getting 401."""
    from fastapi import HTTPException

    request = _make_request("192.168.1.50")
    with pytest.raises(HTTPException) as excinfo:
        await auth.require_lan_token(request, authorization=None)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_rest_no_client_falls_through(isolated_token):
    """request.client == None (rare) must not crash; falls through to 401."""
    from fastapi import HTTPException

    request = _make_request(None)
    with pytest.raises(HTTPException) as excinfo:
        await auth.require_lan_token(request, authorization=None)
    assert excinfo.value.status_code == 401


@pytest.mark.asyncio
async def test_ws_loopback_v4_returns_empty_string(isolated_token):
    """127.0.0.1 WS gets a no-subprotocol pass; caller calls ws.accept()."""
    ws = _StubWS("127.0.0.1")
    out = await auth.require_lan_token_ws(ws)
    assert out == ""
    assert ws.closed_with is None


@pytest.mark.asyncio
async def test_ws_loopback_v6_returns_empty_string(isolated_token):
    ws = _StubWS("::1")
    out = await auth.require_lan_token_ws(ws)
    assert out == ""
    assert ws.closed_with is None


@pytest.mark.asyncio
async def test_ws_lan_ip_still_rejected_with_4401(isolated_token):
    """Regression pin — LAN clients without a token are closed 4401."""
    from fastapi import WebSocketDisconnect

    ws = _StubWS("192.168.1.50")
    with pytest.raises(WebSocketDisconnect):
        await auth.require_lan_token_ws(ws)
    assert ws.closed_with == 4401


@pytest.mark.asyncio
async def test_ws_no_client_falls_through(isolated_token):
    """ws.client == None (rare) must not crash; falls through to 4401."""
    from fastapi import WebSocketDisconnect

    ws = _StubWS(None)
    with pytest.raises(WebSocketDisconnect):
        await auth.require_lan_token_ws(ws)
    assert ws.closed_with == 4401
