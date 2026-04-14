# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Roboot

A personal AI agent hub that runs on macOS. It manages multiple Claude Code sessions in iTerm2, executes shell commands, and provides a JARVIS-like voice interface -- all powered by the Arcana agent framework. Supports remote access via LAN, Telegram, and a Cloudflare Worker relay.

## Architecture

```
server.py (FastAPI)              <- Main entry point
+-- WebSocket /ws                <- Streaming chat (LLM_CHUNK events)
+-- REST /api/sessions/*         <- Direct iTerm2 session control
+-- REST /api/tts                <- Edge TTS (text -> mp3)
+-- REST /api/relay-info         <- Relay pairing URL + expiry
+-- REST /api/relay-refresh      <- Rotate relay token (POST)
+-- REST /api/relay-qr           <- QR code PNG for relay pairing URL
+-- REST /api/network-info       <- Local IP addresses
+-- REST /api/qr-code            <- QR code PNG for LAN URL
+-- Static /static/console.html  <- Unified web console

relay_client.py                  <- Relay WebSocket client (connects to CF Worker)
iterm_bridge.py                  <- Persistent iTerm2 Python API connection
network_utils.py                 <- IP detection, QR code generation
soul.md                          <- Assistant's self-modifiable identity
config.yaml                      <- API keys + provider config (gitignored)

tools/                           <- Arcana tools (agent's capabilities)
+-- shell.py                     <- Terminal command execution
+-- claude_code.py               <- iTerm2 session list/read/send/create
+-- vision.py                    <- Camera capture + screenshot + face recognition
+-- face_db.py                   <- Face encoding storage and matching (.faces/)
+-- soul.py                      <- Self-modification + user memory

adapters/                        <- I/O adapters
+-- telegram_bot.py              <- Remote control via Telegram
+-- voice.py                     <- Local mic STT + macOS TTS
+-- keyboard.py                  <- Terminal text input

relay/                           <- Cloudflare Worker relay server
+-- src/index.ts                 <- Worker entry point, routing, rate limiting
+-- src/relay-session.ts         <- Durable Object: daemon<->client session mgmt
+-- src/pair-page.ts             <- HTML pairing page served to mobile clients
+-- wrangler.toml                <- Cloudflare deployment config
```

## Commands

```bash
# Main web console (primary way to use)
python server.py                    # -> http://localhost:8765

# CLI modes
python run.py                       # Keyboard chat
python run.py --voice               # Voice chat

# Telegram bot (needs bot_token in config.yaml)
python -m adapters.telegram_bot

# Chainlit UI (alternative frontend)
chainlit run chainlit_app.py -w

# Install dependencies
pip install "arcana-agent[all-providers]>=0.4.0" pyyaml fastapi "uvicorn[standard]" edge-tts iterm2 "qrcode[pil]"

# Deploy relay (requires wrangler CLI + Cloudflare account)
cd relay && npm install && npx wrangler deploy
```

## Key Design Decisions

### Agent: Arcana 0.4.0
All LLM interaction goes through Arcana's `Runtime` and `ChatSession`. Tools are registered with `@arcana.tool()` decorator with affordance metadata (`when_to_use`, `what_to_expect`). Streaming uses `session.stream()` which emits `LLM_CHUNK` events (not `TEXT_DELTA`).

### iTerm2 Integration
`iterm_bridge.py` maintains a persistent websocket to iTerm2's Python API. This replaced an earlier AppleScript approach. Requires iTerm2 -> Settings -> General -> Magic -> Enable Python API.

### TTS: Spoken vs Displayed
The model uses `> ` blockquote prefix to mark what should be spoken aloud. `_extract_spoken_text()` in server.py reads only `> ` lines for TTS. Everything else displays on screen only. If model omits `> `, falls back to first sentence.

### Soul System
`soul.md` is the assistant's self-modifiable identity file. The system prompt is built dynamically by `tools/soul.py:build_personality()` which reads soul.md on each new chat session. The assistant can modify its own name, personality, voice, and accumulate knowledge about the user -- all persisted to soul.md.

### Face Recognition
`tools/vision.py` captures camera frames and runs face detection via `face_recognition` library. Known faces are stored in `.faces/faces.json` (encoding vectors) with reference photos in `.faces/photos/`. The `look` tool auto-recognizes faces; `enroll_face` registers new ones. Threshold 0.6 (standard), confidence = 1 - distance/threshold.

### Remote Access Architecture

Three methods, configured in `config.yaml` under `remote_access`:

1. **LAN** (zero config): server binds `0.0.0.0:8765`, QR code displayed at startup. Works on same Wi-Fi.

2. **Telegram Bot** (`adapters/telegram_bot.py`): Standalone process, connects to Telegram Bot API. Supports chat, session management, `/allow` and `/reject` commands. Access controlled by `telegram.allowed_users` list.

3. **Relay** (`relay_client.py` + `relay/`): The local server connects to a Cloudflare Worker relay via WebSocket. Mobile clients connect to the same relay and messages are forwarded bidirectionally. The relay is a Durable Object (`RelaySession`) that manages daemon-to-client routing.

### Token Rotation (Relay)
`relay_client.py` generates a 256-bit random token (`secrets.token_hex(32)`) with a configurable TTL (default 30 minutes). The `rotate_token()` method generates a new session_id + token, clears chat sessions, and closes the old WebSocket (the auto-reconnect loop picks up new credentials). Token expiry is checked on each `_connect()` call. The relay server uses constant-time comparison (`timingSafeEqual`) to verify tokens.

API endpoints for token management:
- `GET /api/relay-info` -- returns pairing URL and expiry timestamp
- `POST /api/relay-refresh` -- rotates token, returns new pairing URL
- `GET /api/relay-qr` -- QR code PNG for current pairing URL

### Streaming Protocol
WebSocket messages from server to frontend:
- `{"type": "thinking"}` -- agent is processing
- `{"type": "delta", "text": "..."}` -- streaming text chunk
- `{"type": "tool_start", "name": "..."}` -- tool execution started
- `{"type": "tool_end", "name": "..."}` -- tool execution ended
- `{"type": "done", "content": "...", "tools_used": N, "sessions": [...]}` -- stream complete
- `{"type": "response", "content": "..."}` -- non-streaming message (welcome)
- `{"type": "error", "content": "..."}` -- error

## Configuration

`config.yaml` (gitignored, copy from `config.example.yaml`):
- `providers`: API keys for deepseek, anthropic, glm, etc.
- `default_provider` / `default_model`: which LLM to use
- `daily_budget_usd`: spending cap
- `voice.tts_voice`: Edge TTS voice name
- `name`, `personality`: assistant identity (also editable via soul.md)
- `telegram.bot_token`, `telegram.allowed_users`: Telegram remote access
- `remote_access.method`: "none" | "official_relay" | "custom_relay"
- `remote_access.relay.enabled`, `remote_access.relay.endpoint`: relay connection settings

`soul.md` (committed):
- Assistant identity, personality, voice
- User knowledge (accumulated through conversation)
- Self-notes

## Adding a New Tool

1. Create `tools/my_tool.py`
2. Use `@arcana.tool()` with `when_to_use` describing when the agent should call it
3. Import and add to `ALL_TOOLS` list in `server.py`
4. No other changes needed -- Arcana auto-registers tools

## Provider Notes

- **DeepSeek**: Default. Stable tool calling, very cheap (~$0.14/M tokens)
- **GLM**: Backup. Tool calling inconsistent with glm-4-flash, works better with explicit prompts
- **Arcana streaming events**: Use `LLM_CHUNK` not `TEXT_DELTA` for text streaming
