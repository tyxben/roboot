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

import arcana

from tools.shell import shell
from tools.claude_code import (
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
)
from tools.vision import look, screenshot
from tools.soul import update_self, remember_user, add_note, build_personality, get_name, get_voice

ALL_TOOLS = [
    shell,
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
    look,
    screenshot,
    update_self,
    remember_user,
    add_note,
]

app = FastAPI(title="Roboot")

STATIC_DIR = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# --- Config & Runtime (created once) ---

_runtime: arcana.Runtime | None = None


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
    If no > lines found, fall back to first short sentence.
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

    # Fallback: no > markers, take first sentence only
    clean = re.sub(r"```[\s\S]*?```", "", text)
    clean = re.sub(r"\*\*(.+?)\*\*", r"\1", clean)
    lines = [l.strip() for l in clean.split("\n") if l.strip() and not l.strip().startswith(("-", "*", "|", "#", "1.", "2.", "3."))]
    if not lines:
        return ""

    first = lines[0]
    # Cut at first sentence end
    m = re.search(r"[。！？.!?]", first)
    if m and m.end() < len(first):
        return first[: m.end()]
    return first[:80]


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

    print("Roboot Web UI: http://localhost:8765")
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")
