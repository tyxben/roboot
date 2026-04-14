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
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from network_utils import get_primary_ip, get_local_ip_addresses, generate_qr_code, generate_qr_ascii

import arcana

from tools.shell import shell
from tools.claude_code import (
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
)
from tools.vision import look, screenshot, enroll_face, list_faces, forget_face
from tools.soul import update_self, remember_user, add_note, build_personality, get_name, get_voice

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


@app.get("/api/sessions")
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


@app.get("/api/sessions/{session_id}/read")
async def api_read_session(session_id: str):
    """Read last N lines from a session via iTerm2 Python API."""
    content = await bridge.read_session(session_id, num_lines=150)
    return {"content": content}


@app.get("/api/network-info")
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
        url = f"{protocol}://{primary_ip}:8765"
        result["urls"].append(url)
        result["qr_url"] = url

    return result


@app.get("/api/relay-info")
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


@app.post("/api/relay-refresh")
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


@app.get("/api/qr-code")
async def api_qr_code():
    """Generate QR code PNG for the primary network URL."""
    primary_ip = get_primary_ip()
    if not primary_ip:
        return Response(content=b"", media_type="image/png")

    cert_file = Path(__file__).parent / "certs" / "cert.pem"
    protocol = "https" if cert_file.exists() else "http"
    url = f"{protocol}://{primary_ip}:8765"
    qr_bytes = generate_qr_code(url, size=8)
    return Response(content=qr_bytes, media_type="image/png")


@app.get("/api/relay-qr")
async def api_relay_qr():
    """Generate QR code PNG for the relay pairing URL."""
    global _relay_client
    if not _relay_client:
        return Response(content=b"", media_type="image/png")
    qr_bytes = generate_qr_code(_relay_client.pairing_url, size=6)
    return Response(content=qr_bytes, media_type="image/png")


@app.post("/api/sessions/{session_id}/send")
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


def _extract_spoken_text(text: str) -> str:
    """Extract lines marked with > (blockquote) — the model's chosen spoken words.

    The model is prompted to use > for what should be said aloud.
    If no > lines found, fall back to first few sentences (up to 300 chars).
    """
    import re

    if not text:
        return ""

    # Primary: extract > blockquote lines (the model's spoken output)
    spoken_lines = []
    for line in text.split("\n"):
        if line.startswith("> "):
            spoken_lines.append(line[2:].strip())

    if spoken_lines:
        result = " ".join(spoken_lines)
        # Strip any remaining markdown
        result = re.sub(r"\*\*(.+?)\*\*", r"\1", result)
        result = re.sub(r"`([^`]+)`", r"\1", result)
        return result.strip()

    # Fallback: no > markers, take first few sentences (up to 300 chars)
    clean = re.sub(r"```[\s\S]*?```", "", text)
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)
    lines = [l.strip() for l in clean.split("\n") if l.strip() and not l.strip().startswith(("-", "*", "|", "#", "1.", "2.", "3."))]
    if not lines:
        return ""

    # Collect multiple sentences up to 300 chars
    spoken_text = ""
    for line in lines:
        if len(spoken_text) + len(line) > 300:
            break
        spoken_text += line + " "
        # Stop at natural paragraph break
        if line.endswith(("。", "！", "？", ".", "!", "?")):
            continue

    # Trim to reasonable length
    spoken_text = spoken_text.strip()
    if len(spoken_text) > 300:
        # Cut at last sentence boundary within 300 chars
        spoken_text = spoken_text[:300]
        m = re.search(r".*[。！？.!?]", spoken_text)
        if m:
            spoken_text = m.group()

    return spoken_text


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


@app.post("/api/tts")
async def api_tts(body: dict):
    """Convert text to speech. Extracts spoken part automatically."""
    raw = body.get("text", "")
    text = _extract_spoken_text(raw)
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
    await ws.accept()
    runtime = _get_runtime()
    # Build personality fresh from soul.md each session
    personality = build_personality()
    session = runtime.create_chat_session(system_prompt=personality)

    name = get_name()
    await ws.send_json({"type": "response", "content": f"Hey，我是 {name}。有什么事？"})

    while True:
        try:
            data = await ws.receive_text()
            msg = json.loads(data)
            user_text = msg.get("content", "").strip()
            if not user_text:
                continue

            await ws.send_json({"type": "thinking"})

            full_text = ""
            tools_used = 0

            async for event in session.stream(user_text):
                etype = str(event.event_type)

                if "LLM_CHUNK" in etype and event.content:
                    full_text += event.content
                    await ws.send_json({
                        "type": "delta",
                        "text": event.content,
                    })

                elif "TOOL_START" in etype or "TOOL_CALL_START" in etype:
                    tools_used += 1
                    await ws.send_json({
                        "type": "tool_start",
                        "name": event.tool_name or "",
                    })

                elif "TOOL_END" in etype or "TOOL_RESULT" in etype:
                    await ws.send_json({
                        "type": "tool_end",
                        "name": event.tool_name or "",
                    })

                elif "RUN_COMPLETE" in etype and event.content:
                    # Final content from run_complete as fallback
                    if not full_text:
                        full_text = event.content

            # Send final complete message
            resp_data = {
                "type": "done",
                "content": full_text,
                "tools_used": tools_used,
            }

            if tools_used > 0:
                try:
                    all_sessions = await bridge.list_sessions()
                    resp_data["sessions"] = [
                        {"id": s.session_id, "project": s.project, "name": s.name}
                        for s in all_sessions
                    ]
                except Exception:
                    pass

            await ws.send_json(resp_data)

        except WebSocketDisconnect:
            break
        except Exception as e:
            await ws.send_json({"type": "error", "content": str(e)})


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
    print(f"   {protocol}://localhost:8765")

    # Network access with QR code
    primary_ip = get_primary_ip()
    if primary_ip:
        network_url = f"{protocol}://{primary_ip}:8765"
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
                    print(f"   {protocol}://{ip}:8765")
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
            build_personality=build_personality,
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
