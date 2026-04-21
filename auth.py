"""Per-daemon LAN bearer token.

The local FastAPI server listens on 0.0.0.0 so anything on the same Wi-Fi
can reach it. To stop a roommate / coffee-shop neighbour / misconfigured
device from driving the iTerm2 sessions, revoking relay pairings, or
pumping audio through the Mac's speakers, every `/api/*` route and the
`/ws` WebSocket require a bearer token. The token is generated on first
run, persisted to ``.auth/lan_token`` with 0600 perms, and embedded into
the startup QR code so legitimate browsers can pick it up one-tap.

Design constraints:
    * stdlib only (``secrets`` + ``hmac``); no passlib / jose.
    * Token lives on disk so that restarts don't invalidate already-paired
      browsers (which have the token in ``localStorage``).
    * REST transport: ``Authorization: Bearer <token>``.
    * WebSocket transport: ``Sec-WebSocket-Protocol: bearer.<token>``
      subprotocol. Query-string tokens are a last-ditch fallback only —
      uvicorn logs query strings, so we prefer never to put the secret
      there.
    * Constant-time comparison via ``hmac.compare_digest``.
"""

from __future__ import annotations

import hmac
import os
import secrets
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse, urlencode, parse_qsl

from fastapi import Header, HTTPException, Request, WebSocket, status


# Where the token lives on disk. Overridable by tests.
AUTH_DIR: Path = Path(__file__).parent / ".auth"
TOKEN_PATH: Path = AUTH_DIR / "lan_token"

# Cached in-process so every request doesn't re-read the file.
_cached_token: Optional[str] = None


def _read_token_file(path: Path) -> Optional[str]:
    """Return the token if the file exists and looks well-formed, else None."""
    if not path.exists():
        return None
    try:
        raw = path.read_text().strip()
    except OSError:
        return None
    # token_urlsafe(32) is 43 chars (base64-url of 32 random bytes, no
    # padding). We require at least 32 chars of URL-safe content to
    # consider the file usable; anything shorter/empty means regenerate.
    if len(raw) < 32:
        return None
    # URL-safe base64 alphabet — reject anything with surprise characters.
    safe = set(
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "abcdefghijklmnopqrstuvwxyz"
        "0123456789-_"
    )
    if any(c not in safe for c in raw):
        return None
    return raw


def load_or_generate_token() -> str:
    """Return the LAN bearer token, generating + persisting it on first call.

    Subsequent calls within the same process hit the cache. The file is
    written with 0600 perms so other LAN users on a shared Mac can't read
    it. Malformed/empty files are regenerated rather than trusted.
    """
    global _cached_token
    if _cached_token:
        return _cached_token

    existing = _read_token_file(TOKEN_PATH)
    if existing:
        _cached_token = existing
        return existing

    AUTH_DIR.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    # Write then chmod (chmod-on-open would need os.open flags; this is
    # simple and the window between write + chmod is negligible for a LAN
    # daemon's threat model).
    TOKEN_PATH.write_text(token)
    try:
        TOKEN_PATH.chmod(0o600)
    except OSError:
        # Non-POSIX filesystems (Windows, some network mounts) may reject
        # chmod. The token is still secret-enough; just press on.
        pass
    _cached_token = token
    return token


def _reset_cache_for_tests() -> None:
    """Forget the cached token. Tests use this when repointing TOKEN_PATH."""
    global _cached_token
    _cached_token = None


def _extract_bearer_from_subprotocol(ws: WebSocket) -> Optional[str]:
    """Pull the token out of a ``bearer.<token>`` Sec-WebSocket-Protocol.

    Browsers send the subprotocol list comma-separated in a single header;
    FastAPI parses them into ``ws.scope["subprotocols"]`` but falls back
    to the raw header otherwise.
    """
    protocols = ws.scope.get("subprotocols") or []
    if not protocols:
        raw = ws.headers.get("sec-websocket-protocol", "")
        if raw:
            protocols = [p.strip() for p in raw.split(",") if p.strip()]
    for proto in protocols:
        if proto.startswith("bearer."):
            return proto[len("bearer."):]
    return None


def _compare(provided: str, expected: str) -> bool:
    """Constant-time string compare that tolerates wrong-length inputs."""
    try:
        return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))
    except (AttributeError, TypeError):
        return False


async def require_lan_token(
    request: Request,
    authorization: Optional[str] = Header(None),
) -> None:
    """FastAPI dependency — 401 unless the request carries the LAN token.

    Accepts:
        * ``Authorization: Bearer <token>`` (preferred, not logged).
        * ``?token=<token>`` query param (fallback; uvicorn logs it, so
          the console.html frontend strips it immediately after first
          load).
    """
    expected = load_or_generate_token()

    token: Optional[str] = None
    if authorization:
        parts = authorization.split(None, 1)
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1].strip()

    if token is None:
        token = request.query_params.get("token")

    if not token or not _compare(token, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="missing or invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


async def require_lan_token_ws(ws: WebSocket) -> str:
    """WebSocket equivalent of ``require_lan_token``.

    Returns the subprotocol to echo back on ``accept()`` so the browser's
    ``new WebSocket(url, 'bearer.<token>')`` call succeeds. Callers must
    pass that string to ``ws.accept(subprotocol=...)``. Closes the socket
    with code 4401 and raises ``WebSocketDisconnect`` on failure so the
    endpoint coroutine exits cleanly.
    """
    from fastapi import WebSocketDisconnect

    expected = load_or_generate_token()
    token = _extract_bearer_from_subprotocol(ws)
    subprotocol: Optional[str] = None
    if token is not None:
        subprotocol = f"bearer.{token}"
    else:
        # Fallback — query string. Not recommended (uvicorn logs it) but
        # unavoidable for older clients or tooling that can't set
        # subprotocols.
        token = ws.query_params.get("token")

    if not token or not _compare(token, expected):
        # RFC 6455 reserves 4000-4999 for application use. 4401 mirrors
        # HTTP 401 for operators skimming logs.
        await ws.close(code=4401)
        raise WebSocketDisconnect(code=4401)
    return subprotocol or ""


def attach_token_to_url(url: str) -> str:
    """Return ``url`` with ``?token=<lan_token>`` appended.

    Used when building the LAN banner URL + QR code so a freshly-scanned
    browser gets the token on first load and can persist it. Existing
    query params (if any) are preserved; an existing ``token=`` is
    overwritten with the current value.
    """
    token = load_or_generate_token()
    parts = urlparse(url)
    query_pairs = [(k, v) for (k, v) in parse_qsl(parts.query, keep_blank_values=True) if k != "token"]
    query_pairs.append(("token", token))
    return urlunparse(parts._replace(query=urlencode(query_pairs)))
