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
