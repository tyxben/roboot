"""Roboot Web Server — FastAPI + WebSocket chat interface."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

import subprocess

import yaml
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
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

ALL_TOOLS = [
    shell,
    list_sessions,
    read_session,
    send_to_session,
    create_claude_session,
    look,
    screenshot,
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
        _runtime._personality = config.get("personality", "")
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


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    runtime = _get_runtime()

    async with runtime.chat(system_prompt=runtime._personality) as session:
        # Welcome
        await ws.send_json({"type": "response", "content": "你好，我是 Roboot。"})

        while True:
            try:
                data = await ws.receive_text()
                msg = json.loads(data)
                user_text = msg.get("content", "").strip()
                if not user_text:
                    continue

                # Show thinking
                await ws.send_json({"type": "thinking"})

                response = await session.send(user_text)

                # Check if response references sessions → send session list for UI actions
                resp_data = {
                    "type": "response",
                    "content": response.content,
                    "tools_used": response.tool_calls_made,
                    "tokens": response.tokens_used,
                    "cost": response.cost_usd,
                }

                # If tools were used, include current sessions so frontend can offer "open" buttons
                if response.tool_calls_made and response.tool_calls_made > 0:
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
