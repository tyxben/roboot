"""Roboot Web Server — FastAPI + WebSocket chat interface."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import subprocess

import tempfile

import edge_tts
import yaml
from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from auth import (
    attach_token_to_url,
    load_or_generate_token,
    require_lan_token,
    require_lan_token_ws,
)
from network_utils import get_primary_ip, get_local_ip_addresses, generate_qr_code, generate_qr_ascii
from text_utils import extract_spoken_text

import arcana

from tools.shell import shell
from tools.claude_code import (
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
)
from tools.vision import look, screenshot, enroll_face, list_faces, forget_face
from tools.soul import (
    update_self,
    remember_user,
    add_note,
    build_personality,
    get_name,
    get_voice,
    summarize_sessions,
)

ALL_TOOLS = [
    shell,
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
    look,
    screenshot,
    enroll_face,
    list_faces,
    forget_face,
    update_self,
    remember_user,
    add_note,
]

app = FastAPI(title="Roboot")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Config & Runtime (created once) ---

_runtime: arcana.Runtime | None = None
_relay_client = None  # Set when relay is enabled


def _load_config() -> dict:
    config_path = Path(__file__).parent / "config.yaml"
    if config_path.exists():
        return yaml.safe_load(config_path.read_text()) or {}
    return {
        "providers": {
            "deepseek": os.environ.get("DEEPSEEK_API_KEY", ""),
        },
        "default_provider": "deepseek",
        "personality": "你是 Roboot，一个简洁友好的 AI 助手。",
    }


def _get_runtime() -> arcana.Runtime:
    global _runtime
    if _runtime is None:
        config = _load_config()
        providers = {
            k: v
            for k, v in config.get("providers", {}).items()
            if v and not v.startswith("sk-...")
        }
        _runtime = arcana.Runtime(
            providers=providers,
            tools=ALL_TOOLS,
            budget=arcana.Budget(max_cost_usd=config.get("daily_budget_usd", 5.0)),
            config=arcana.RuntimeConfig(
                default_provider=config.get("default_provider", "deepseek"),
                default_model=config.get("default_model"),
            ),
        )
    return _runtime


# --- Routes ---


@app.get("/")
async def index():
    return FileResponse(STATIC_DIR / "console.html")


@app.get("/chat-only")
async def chat_only():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/dashboard")
async def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html")


@app.get("/session")
async def session_page():
    return FileResponse(STATIC_DIR / "session.html")


# --- Dashboard API (iTerm2 Python API) ---

from iterm_bridge import bridge
from chat_handler import handle_chat
from session_watcher import watcher as _session_watcher
import chat_store
import memory

# Active web-console WebSockets. The session watcher, self-upgrade loop,
# and any future broadcaster push `notify` frames through here.
_active_ws_clients: set[WebSocket] = set()

# Number of chat turns currently mid-stream. Incremented just before
# handle_chat() is awaited, decremented in finally. The self-upgrade loop
# reads this via get_in_flight_count() and defers restarts while > 0 so it
# never kills a user's in-progress response. Single-threaded in the event
# loop, so plain int mutation is fine.
_chat_in_flight: int = 0


def get_in_flight_count() -> int:
    """Number of chat turns currently executing in the /ws endpoint."""
    return _chat_in_flight


async def _relay_broadcast(relay, frame: dict) -> None:
    """Encrypt and push `frame` to every paired relay client (best-effort)."""
    for cid in list(getattr(relay, "_ciphers", {}).keys()):
        try:
            await relay._send_to_client(cid, frame)
        except Exception:
            pass


@app.get("/api/sessions", dependencies=[Depends(require_lan_token)])
async def api_list_sessions():
    """List all iTerm2 sessions."""
    sessions = await bridge.list_sessions()
    return {
        "sessions": [
            {
                "id": s.session_id,
                "project": s.project,
                "name": s.name,
                "pid": s.pid,
                "tty": s.tty,
                "cwd": s.cwd,
            }
            for s in sessions
        ]
    }


@app.get("/api/sessions/{session_id}/read", dependencies=[Depends(require_lan_token)])
async def api_read_session(session_id: str, color: bool = False, after: int | None = None):
    """Read lines from a session via iTerm2 Python API.

    color=true returns ANSI-escaped content for frontend colorization
    (via ansi_up or similar). Default stays plain for backwards compat
    with existing consumers.

    after=<int> switches to incremental mode: returns only lines with
    absolute line number greater than `after`, plus bookkeeping fields
    (`last_line`, `overflow`, `dropped_prefix`). When `after` is omitted,
    behavior matches the prior API but the JSON body now also always
    includes `last_line` so clients can seamlessly pivot to incremental
    polling. Unknown-field-tolerant consumers are unaffected.
    """
    if after is not None:
        result = await bridge.read_session_incremental(
            session_id, after_line=after, color=color
        )
        return result

    # Non-incremental path — use the incremental reader internally so we
    # can report last_line back to the frontend for its first poll.
    initial = await bridge.read_session_incremental(
        session_id, after_line=None, num_lines_initial=1000, color=color
    )
    if "error" in initial and "content" not in initial:
        return {"content": initial["error"], "last_line": -1}
    return {
        "content": initial.get("content", ""),
        "last_line": initial.get("last_line", -1),
    }


@app.get("/api/network-info", dependencies=[Depends(require_lan_token)])
async def api_network_info():
    """Get network information including local IPs and QR code for mobile access."""
    primary_ip = get_primary_ip()
    all_ips = get_local_ip_addresses()

    # Detect if SSL is enabled
    cert_file = Path(__file__).parent / "certs" / "cert.pem"
    protocol = "https" if cert_file.exists() else "http"

    result = {
        "primary_ip": primary_ip,
        "all_ips": all_ips,
        "urls": [],
        "ssl_enabled": cert_file.exists(),
    }

    if primary_ip:
        url = attach_token_to_url(f"{protocol}://{primary_ip}:8765")
        result["urls"].append(url)
        result["qr_url"] = url

    return result


@app.get("/api/relay-info", dependencies=[Depends(require_lan_token)])
async def api_relay_info():
    """Get relay pairing URL if relay is enabled."""
    global _relay_client
    if _relay_client:
        return {
            "enabled": True,
            "pairing_url": _relay_client.pairing_url,
            "expires_at": _relay_client.token_created_at + _relay_client.token_ttl,
        }
    return {"enabled": False, "pairing_url": None}


@app.post("/api/relay-refresh", dependencies=[Depends(require_lan_token)])
async def api_relay_refresh():
    """Rotate relay pairing token and return new pairing URL."""
    global _relay_client
    if _relay_client:
        _relay_client.rotate_token()
        return {
            "enabled": True,
            "pairing_url": _relay_client.pairing_url,
            "expires_at": _relay_client.token_created_at + _relay_client.token_ttl,
        }
    return {"enabled": False}


@app.post("/api/relay-revoke", dependencies=[Depends(require_lan_token)])
async def api_relay_revoke():
    """Revoke all remote access: broadcast to clients, close their sockets,
    invalidate the current pairing token, and rotate to a fresh link."""
    global _relay_client
    if _relay_client:
        _relay_client.revoke_all()
        return {
            "enabled": True,
            "revoked": True,
            "pairing_url": _relay_client.pairing_url,
            "expires_at": _relay_client.token_created_at + _relay_client.token_ttl,
        }
    return {"enabled": False, "revoked": False}


@app.get("/api/qr-code", dependencies=[Depends(require_lan_token)])
async def api_qr_code():
    """Generate QR code PNG for the primary network URL.

    The QR encodes the URL WITH the LAN bearer token so a fresh phone
    scan can complete pairing in one tap. Because the token is embedded,
    this endpoint itself is gated — callers must already possess the
    token (e.g. the console loaded from the startup URL).
    """
    primary_ip = get_primary_ip()
    if not primary_ip:
        return Response(content=b"", media_type="image/png")

    cert_file = Path(__file__).parent / "certs" / "cert.pem"
    protocol = "https" if cert_file.exists() else "http"
    url = attach_token_to_url(f"{protocol}://{primary_ip}:8765")
    qr_bytes = generate_qr_code(url, size=8)
    return Response(content=qr_bytes, media_type="image/png")


@app.get("/api/relay-qr", dependencies=[Depends(require_lan_token)])
async def api_relay_qr():
    """Generate QR code PNG for the relay pairing URL."""
    global _relay_client
    if not _relay_client:
        return Response(content=b"", media_type="image/png")
    qr_bytes = generate_qr_code(_relay_client.pairing_url, size=6)
    return Response(content=qr_bytes, media_type="image/png")


@app.post("/api/sessions/{session_id}/send", dependencies=[Depends(require_lan_token)])
async def api_send_to_session(session_id: str, body: dict):
    """Send text to a session via iTerm2 Python API."""
    text = body.get("text", "")
    if not text:
        return {"result": "内容为空"}
    result = await bridge.send_text(session_id, text)
    if result == "sent":
        return {"result": "已发送"}
    return {"result": result}


TTS_VOICE_DEFAULT = "zh-CN-YunxiNeural"


@app.get("/static/cert.pem")
async def download_cert():
    """Download SSL certificate for manual trust (optional)."""
    cert_path = Path(__file__).parent / "certs" / "cert.pem"
    if cert_path.exists():
        return FileResponse(
            cert_path,
            media_type="application/x-pem-file",
            filename="roboot-cert.pem"
        )
    return {"error": "Certificate not found"}


@app.post("/api/tts", dependencies=[Depends(require_lan_token)])
async def api_tts(body: dict):
    """Convert text to speech. Extracts spoken part automatically."""
    raw = body.get("text", "")
    text = extract_spoken_text(raw)
    if not text:
        return Response(content=b"", media_type="audio/mpeg")

    voice = get_voice() or TTS_VOICE_DEFAULT
    comm = edge_tts.Communicate(text, voice=voice, rate="+10%")
    audio_bytes = b""
    async for chunk in comm.stream():
        if chunk["type"] == "audio":
            audio_bytes += chunk["data"]

    return Response(content=audio_bytes, media_type="audio/mpeg")


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    # Auth happens before accept() so unauthenticated connections are
    # rejected at handshake time without the server ever echoing a
    # subprotocol. A valid token produces a "bearer.<token>"
    # subprotocol that we must echo back, or browsers will close with
    # "failed to execute 'WebSocket'".
    try:
        subprotocol = await require_lan_token_ws(ws)
    except WebSocketDisconnect:
        return
    if subprotocol:
        await ws.accept(subprotocol=subprotocol)
    else:
        await ws.accept()
    _active_ws_clients.add(ws)
    runtime = _get_runtime()
    # Build personality fresh from soul.md each session
    sessions_summary = await summarize_sessions()
    personality = build_personality(channel="web", sessions_summary=sessions_summary)
    session = runtime.create_chat_session(system_prompt=personality)
    # Layer A: if this is a reconnect carrying a prior history_session_id,
    # replay the last few turns so the agent doesn't greet the user as if
    # they never met. New connections (no prior id) get a fresh row.
    prior_history_id: str | None = None
    history_session_id = await chat_store.create_session(source="local")

    name = get_name()
    await ws.send_json({"type": "response", "content": f"Hey，我是 {name}。有什么事？"})

    try:
        while True:
            try:
                data = await ws.receive_text()
                msg = json.loads(data)
                # A client that wants to resume sends `resume_session_id` on
                # its first payload. We replay once, then stick to the new
                # history_session_id going forward so we don't double-record.
                if prior_history_id is None:
                    rid = msg.get("resume_session_id")
                    if rid:
                        prior_history_id = rid
                        try:
                            await memory.replay_history(session, rid)
                        except Exception as e:
                            print(f"[memory] replay_history failed: {e}")
                user_text = msg.get("content", "").strip()
                if not user_text:
                    continue

                global _chat_in_flight
                _chat_in_flight += 1
                try:
                    await handle_chat(
                        session,
                        user_text,
                        ws.send_json,
                        history_session_id=history_session_id,
                    )
                finally:
                    _chat_in_flight -= 1
                # Layer B: count turns; when the window fills, schedule a
                # background distillation pass.
                memory.record_turn_and_maybe_distill(
                    history_session_id, runtime=runtime
                )

            except WebSocketDisconnect:
                break
            except Exception as e:
                await ws.send_json({"type": "error", "content": str(e)})
    finally:
        _active_ws_clients.discard(ws)


async def _broadcast_waiting_notification(payload: dict) -> None:
    """Session-watcher subscriber: push a notify frame to every active console.

    Also forwards to connected relay clients when the relay is running, so
    remote consoles get the same proactive heads-up as local ones.
    """
    text = f"🔔 Session {payload.get('project', '?')}: {payload.get('prompt_line', '')}"
    frame = {
        "type": "notify",
        "text": text,
        "session_id": payload.get("session_id"),
        "project": payload.get("project"),
        "prompt_line": payload.get("prompt_line"),
    }
    # Local web console sockets.
    dead: list[WebSocket] = []
    for client_ws in list(_active_ws_clients):
        try:
            await client_ws.send_json(frame)
        except Exception:
            dead.append(client_ws)
    for client_ws in dead:
        _active_ws_clients.discard(client_ws)

    # Relay-connected mobile clients. The relay client lives in a separate
    # thread with its own event loop; hop the coroutine onto it.
    global _relay_client
    relay = _relay_client
    if relay is not None and getattr(relay, "_loop", None) is not None:
        try:
            asyncio.run_coroutine_threadsafe(
                _relay_broadcast(relay, frame), relay._loop
            )
        except Exception:
            pass


async def _relay_broadcast(relay, frame: dict) -> None:
    """Encrypt and push `frame` to every paired relay client."""
    for cid in list(getattr(relay, "_ciphers", {}).keys()):
        try:
            await relay._send_to_client(cid, frame)
        except Exception:
            pass


@app.on_event("startup")
async def _start_session_watcher():
    _session_watcher.subscribe(_broadcast_waiting_notification)
    _session_watcher.start()


@app.on_event("startup")
async def _start_self_upgrade_loop():
    """Opt-in code self-upgrade loop.

    Gated on ``ROBOOT_AUTO_UPGRADE=1`` so dev checkouts and CI never
    auto-pull. See ``self_upgrade.py`` for the full rationale and
    failure-handling contract.
    """
    if os.environ.get("ROBOOT_AUTO_UPGRADE") == "1":
        from self_upgrade import run_upgrade_loop

        asyncio.create_task(run_upgrade_loop(app))


@app.on_event("shutdown")
async def shutdown():
    global _runtime
    if _runtime:
        await _runtime.close()
        _runtime = None


if __name__ == "__main__":
    import uvicorn

    # Check for SSL certificates
    cert_file = Path(__file__).parent / "certs" / "cert.pem"
    key_file = Path(__file__).parent / "certs" / "key.pem"
    use_ssl = cert_file.exists() and key_file.exists()
    protocol = "https" if use_ssl else "http"

    # Display startup banner with network information
    print("\n" + "=" * 60)
    print("🤖 Roboot - Personal AI Agent Hub")
    print("=" * 60)

    if use_ssl:
        print("\n🔒 SSL/TLS: ENABLED (HTTPS + WSS)")
        print("   ✅ Encrypted communication")
        print("   ✅ Camera & voice enabled")
    else:
        print("\n⚠️  SSL/TLS: DISABLED (HTTP only)")
        print("   ❌ Camera & voice require HTTPS")
        print("   💡 Run: python -m tools.generate_cert to enable SSL")

    # Local access
    print(f"\n📍 Local access:")
    print(f"   {attach_token_to_url(f'{protocol}://localhost:8765')}")

    # Pre-warm the LAN token so the banner URL + QR carry it.
    load_or_generate_token()

    # Network access with QR code
    primary_ip = get_primary_ip()
    if primary_ip:
        network_url = attach_token_to_url(f"{protocol}://{primary_ip}:8765")
        print(f"\n📱 Mobile access (scan QR code):")
        print(f"   {network_url}")

        if use_ssl:
            print("\n   ⚠️  First time: Trust the certificate on your phone")
            print("   📖 See docs/ssl-trust-guide.md for instructions")
        print()

        # Generate and display QR code in terminal
        try:
            qr_ascii = generate_qr_ascii(network_url)
            print(qr_ascii)
            print()
        except Exception as e:
            print(f"   (QR code generation failed: {e})")

        all_ips = get_local_ip_addresses()
        if len(all_ips) > 1:
            print("\n🌐 Other network interfaces:")
            for ip in all_ips:
                if ip != primary_ip:
                    print(f"   {attach_token_to_url(f'{protocol}://{ip}:8765')}")
    else:
        print("\n⚠️  No network interface detected (localhost only)")

    # Optional: Connect to relay for remote access
    config = _load_config()
    relay_config = config.get("remote_access", {})
    relay_method = relay_config.get("method", "none")
    relay_enabled = (
        relay_method in ("official_relay", "custom_relay")
        or relay_config.get("relay", {}).get("enabled", False)
    )

    if relay_enabled:
        relay_url = relay_config.get("relay", {}).get(
            "endpoint", "wss://relay.coordbound.com"
        )

        from relay_client import RelayClient

        relay = RelayClient(
            relay_url=relay_url,
            runtime=_get_runtime(),
            # Remote clients reach the relay via the mobile web console, so
            # "web" is still the right channel label here.
            build_personality=lambda: build_personality(channel="web"),
            get_name=get_name,
        )
        _relay_client = relay

        pairing_url = relay.pairing_url
        print(f"\n\U0001F30D Remote access (relay):")
        print(f"   {pairing_url}")
        print()
        try:
            qr_ascii = generate_qr_ascii(pairing_url)
            print(qr_ascii)
        except Exception:
            pass

        # Start relay in a background thread with its own event loop
        import threading

        def _run_relay():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            loop.run_until_complete(relay.start())

        relay_thread = threading.Thread(target=_run_relay, daemon=True)
        relay_thread.start()
        print("\n\u2705 Relay started -- remote access enabled")

    print("\n" + "=" * 60 + "\n")

    # Start server - bind to 0.0.0.0 to accept connections from network
    if use_ssl:
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8765,
            log_level="warning",
            ssl_keyfile=str(key_file),
            ssl_certfile=str(cert_file),
        )
    else:
        uvicorn.run(app, host="0.0.0.0", port=8765, log_level="warning")
