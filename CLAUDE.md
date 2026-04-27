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
+-- REST /api/relay-revoke       <- Kick all clients + rotate (POST)
+-- REST /api/relay-qr           <- QR code PNG for relay pairing URL
+-- REST /api/network-info       <- Local IP addresses
+-- REST /api/qr-code            <- QR code PNG for LAN URL
+-- REST /api/filevault-status   <- macOS FileVault probe (fdesetup)
+-- REST /api/chat-history-wipe  <- Drop all chat rows + VACUUM (POST)
+-- Static /static/console.html  <- Unified web console

relay_client.py                  <- Relay WebSocket client (connects to CF Worker)
iterm_bridge.py                  <- Persistent iTerm2 Python API connection
network_utils.py                 <- IP detection, QR code generation
soul.md                          <- Assistant's self-modifiable identity
config.yaml                      <- API keys + provider config (gitignored)

text_utils.py                    <- Shared helpers (extract_spoken_text, ...)
tts_synth.py                     <- Edge TTS helper shared by /api/tts + mobile relay
soul_review.py                   <- Review gate for soul.md overwrites (off/log/confirm)
tool_guard.py                    <- Approval gate for agent tool calls (off/log/confirm) — gates run_command via Arcana confirmation_callback
filevault_status.py              <- macOS fdesetup probe for the console warning banner

tools/                           <- Arcana tools (agent's capabilities)
+-- shell.py                     <- Terminal command execution
+-- claude_code.py               <- iTerm2 session list/read/send/create
+-- vision.py                    <- Camera capture + screenshot + face recognition
+-- face_db.py                   <- Face encoding storage and matching (.faces/)
+-- soul.py                      <- Self-modification + user memory
+-- voice_switch.py              <- Agent tool: change Telegram TTS voice

adapters/                        <- I/O adapters
+-- telegram_bot.py              <- Remote control via Telegram (voice I/O)
+-- voice.py                     <- Local mic STT + macOS TTS (CLI --voice)
+-- voice_prefs.py               <- Per-Telegram-user TTS voice store
+-- tts_streamer.py              <- Edge TTS -> parallel OGG/Opus synthesis
+-- keyboard.py                  <- Terminal text input
+-- stt/                         <- Pluggable speech-to-text backends
    +-- mlx.py                   <- mlx-whisper (Apple Silicon, default)
    +-- google.py                <- speech_recognition -> Google Web Speech
    +-- noop.py                  <- backend: none

scripts/
+-- setup.sh                     <- One-command installer (auto-uses uv)

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

# Install (recommended: scripts/setup.sh handles deps + ffmpeg + Whisper prewarm)
./scripts/setup.sh                                       # default: telegram extras
./scripts/setup.sh --with=core                           # minimal (no voice)
./scripts/setup.sh --with=all                            # everything

# Manual install (if you don't want setup.sh)
pip install -e .                                         # core
pip install -e '.[telegram]'                             # + Telegram voice I/O
pip install -e '.[all]'                                  # + vision + CLI voice + desktop
python -m adapters.stt prewarm                           # pre-cache Whisper model (~3 GB)

# Deploy relay (requires wrangler CLI + Cloudflare account)
cd relay && npm install && npx wrangler deploy
```

## Key Design Decisions

### Agent: Arcana 0.8.2
All LLM interaction goes through Arcana's `Runtime` and `ChatSession`. Tools are registered with `@arcana.tool()` decorator with affordance metadata (`when_to_use`, `what_to_expect`). Streaming uses `session.stream()` which emits `LLM_CHUNK` events (not `TEXT_DELTA`). Pinned floor `>=0.8.2,<0.9` in `pyproject.toml` — 0.8.x is the "Collaborative Cognition" line; we only use the single-agent surface, so the multi-agent (`runtime.collaborate()`) and cognitive-primitives (`recall`/`pin`) features are available but unused. `Message` / `MessageRole` are imported from `arcana.contracts.llm` (canonical path), not `arcana.runtime.conversation` (re-export, fragile).

### iTerm2 Integration
`iterm_bridge.py` maintains a persistent websocket to iTerm2's Python API. This replaced an earlier AppleScript approach. Requires iTerm2 -> Settings -> General -> Magic -> Enable Python API.

### TTS: Spoken vs Displayed
The model uses `> ` blockquote prefix to mark what should be spoken aloud. `text_utils.extract_spoken_text()` reads only `> ` lines for TTS (shared between `server.py` and the Telegram bot). Everything else displays on screen only. If the model omits `> `, falls back to first sentence.

For Telegram, `adapters/tts_streamer.py` splits the spoken text into up to 3 chunks (Chinese-aware sentence segmenter in `segment_for_tts`), synthesizes them in parallel via Edge TTS, and converts mp3 -> OGG/Opus through ffmpeg so the reply lands as native Telegram voice bubbles. Per-user voice preference via `adapters/voice_prefs.py` — `/voice` picker or `switch_tts_voice` agent tool writes to `.voice_prefs/prefs.json`.

For the mobile relay pair-page, TTS also runs server-side Edge TTS (matches the local console voice). The browser sends `{"type":"tts_request","req_id":N,"text":"..."}` over the encrypted WS; the daemon responds with `{"type":"tts_audio","req_id":N,"mp3_b64":"..."}` which the browser decodes and plays via `HTMLAudioElement`. If the daemon errors or doesn't respond within 4s, the browser falls back to `speechSynthesis`. Both sides share `tts_synth.synthesize_spoken()` so voice selection and spoken-text extraction stay consistent between `/api/tts` and the relay path.

### STT Backends
Speech-to-text is pluggable via the `adapters/stt/` package. The backend is chosen at runtime from `config.yaml` under `voice.stt.backend`:

- `mlx_whisper` (default) -- Apple-Silicon native MLX Whisper. Offline after first model download. Model size configurable via `voice.stt.model` (large-v3 ~3 GB / ~96% zh, medium ~1.5 GB / ~93%, small ~500 MB / ~88%). Env vars `ROBOOT_WHISPER_MODEL` and `ROBOOT_WHISPER_LANGUAGE` override config.
- `google` -- `speech_recognition` against Google's free Web Speech endpoint. No local RAM/disk, requires internet, rate-limited, ~85-90% Chinese accuracy. Intended for Intel Macs or low-RAM environments.
- `none` -- Disables voice input. Telegram replies "STT disabled" when a voice message arrives.

If the `voice.stt` block is absent, Roboot defaults to `mlx_whisper` to preserve pre-v0.4 behavior.

### Soul System
`soul.md` is the assistant's self-modifiable identity file. The system prompt is built dynamically by `tools/soul.py:build_personality()` which reads soul.md on each new chat session. The assistant can modify its own name, personality, voice, and accumulate knowledge about the user -- all persisted to soul.md.

### Soul Review Gate
Every overwrite of `soul.md` (from `update_self` / `remember_user` / `add_note` / the distiller's `append_self_feedback`) goes through `soul_review.review_write()` first, to block prompt-injected agents from silently persisting malicious content. Mode is controlled by `ROBOOT_SOUL_REVIEW`:
- `off` (default) — write proceeds unreviewed, preserving prior behavior.
- `log` — write proceeds, but the unified diff also lands in `.soul/pending/<ts>-<origin>.diff` for after-the-fact audit.
- `confirm` — daemon broadcasts a `{"type":"soul_review","req_id":...,"origin":...,"diff":...}` frame to every connected console (local + paired mobile); modal shows the diff + a countdown + 允许/拒绝 buttons. The user's choice comes back as `{"type":"soul_review_decision","req_id":...,"approved":true|false}`; no reply within `timeout_s` (default 30) counts as REJECTED.

Diffs over 2 KB are always REJECTED (too large to eyeball on a phone). Automated origins (the periodic distiller via `remember_user_automated`, and `append_self_feedback` on its sync path) degrade CONFIRM to LOG so the user isn't modal-spammed every K turns — the diff still gets audited.

### Tool Approval Gate
Same shape as the soul review gate, but it gates the *action* path (the agent calling `run_command` etc.) instead of the *persistence* path (writes to soul.md). Without it, a prompt-injected `session_watcher` tail or Telegram message can chain straight into shell. Mode is controlled by `ROBOOT_TOOL_APPROVAL`:
- `off` (default) — bypass entirely; preserves pre-v0.4 behavior.
- `log` — non-dangerous calls pass through; dangerous calls (matched against `tool_guard.DANGEROUS_PATTERNS` — 38 curated regexes covering rm/dd/sudo/pipe-to-shell/credential dirs/Roboot at-rest paths/etc.) are still allowed but the call lands in `.tool_audit/<ts>-<tool>-LOGGED.json`.
- `confirm` — dangerous calls broadcast `{"type":"tool_approval","req_id":...,"tool":...,"args_summary":...,"danger_reason":...,"origin":...,"issued_at":...,"timeout_s":30}` to every registered surface (local console + relay mobile + Telegram). The reply `{"type":"tool_approval_decision","req_id":...,"approved":bool}` resolves the gate's pending future. No reply within `timeout_s` → REJECTED. Args summary > 2 KB → REJECTED unconditionally.

Hooked into Arcana via `runtime._tool_gateway.confirmation_callback = tool_guard.confirmation_callback` — each gated tool just declares `requires_confirmation=True` on its `@arcana.tool(...)` decorator. Currently gated: `run_command` (shell). The callback fails *closed* on any internal exception (returns False) so a crashing gate rejects the call rather than waving it through.

The danger detector applies NFKC + ANSI strip + null-byte normalization before matching, with a 16 KB hard cap on detector input (ReDoS guard). Allowlist at `~/.roboot/tool_allowlist.json` (per-machine, gitignored) does prefix matching with token boundaries; metachar-containing entries (`;`, `&`, backtick, `$(`, `||`, etc.) are silently rejected at lookup time so a user can't write `prefix: "ls; rm -rf"` and feel safe. Allowlist CANNOT override danger detection — a dangerous shell command goes to modal regardless.

Origin tracking via `tool_guard.current_origin` contextvar (set to `"local"` in `server.py`, `"relay"` in `relay_client.py`, `"telegram"` in `adapters/telegram_bot.py` at message-receive boundary) — surfaces in audit records and the broadcast frame. Telegram only DMs the *triggering* user (read from the existing `current_tg_user` contextvar); cross-user clicks on the inline-keyboard message are rejected with a toast and don't touch the future, so a stranger who learns a `req_id` can't approve another user's tool.

### At-Rest Assumptions
Roboot keeps `config.yaml` (API keys + Telegram token), `.identity/daemon.ed25519.key`, `soul.md`, `.chat_history.db`, and `.faces/faces.json` in plaintext on disk. Per-file encryption was evaluated and dropped as theater — same-user attacker reads everything regardless, and the real defense is **FileVault on the boot volume**. `filevault_status.py` (probes `fdesetup status`, 3 s timeout) feeds `/api/filevault-status`; the console shows a red sticky banner if it returns `{enabled: false}` so the assumption is visible, not silent. Non-macOS and probe failures map to `enabled=None` (banner stays hidden).

Chat-history privacy hygiene is handled by `chat_store.wipe_all()` behind `POST /api/chat-history-wipe` (LAN-token gated) — wired to the 擦除所有聊天 button in the Network panel. It DELETEs all rows from `messages` + `sessions` and runs `VACUUM` on a post-autocommit connection so deleted pages are reclaimed and not recoverable from the file. Active WS connections keep their in-memory `history_session_id`; subsequent message writes just re-create the row. Retention is still governed by `ROBOOT_CHAT_RETENTION_DAYS` (default 30) which runs automatically on every `create_session()`.

### Face Recognition
`tools/vision.py` captures camera frames and runs face detection via `face_recognition` library. Known faces are stored in `.faces/faces.json` (encoding vectors) with reference photos in `.faces/photos/`. The `look` tool auto-recognizes faces; `enroll_face` registers new ones. Threshold 0.6 (standard), confidence = 1 - distance/threshold.

### Remote Access Architecture

Three methods, configured in `config.yaml` under `remote_access`:

1. **LAN** (zero config): server binds `0.0.0.0:8765`, QR code displayed at startup. Works on same Wi-Fi.

2. **Telegram Bot** (`adapters/telegram_bot.py`): Standalone process, connects to Telegram Bot API. Supports text + voice chat (mlx-whisper STT + Edge TTS), session management, and slash commands `/help`, `/sessions`, `/screenshot`, `/voice`, `/remote`, `/refresh`. The agent can also call tools directly on plain-language requests ("截个屏", "换成女声" → `screenshot` / `switch_tts_voice`). Access controlled by `telegram.allowed_users` list.

3. **Relay** (`relay_client.py` + `relay/`): The local server connects to a Cloudflare Worker relay via WebSocket. Mobile clients connect to the same relay and messages are forwarded bidirectionally. The relay is a Durable Object (`RelaySession`) that manages daemon-to-client routing.

### Token Rotation (Relay)
`relay_client.py` generates a 256-bit random token (`secrets.token_hex(32)`) with a configurable TTL (default 30 minutes). The `rotate_token()` method generates a new session_id + token, clears chat sessions, and closes the old WebSocket (the auto-reconnect loop picks up new credentials). Token expiry is checked on each `_connect()` call. The relay server uses constant-time comparison (`timingSafeEqual`) to verify tokens.

API endpoints for token management:
- `GET /api/relay-info` -- returns pairing URL and expiry timestamp
- `POST /api/relay-refresh` -- rotates token, returns new pairing URL
- `GET /api/relay-qr` -- QR code PNG for current pairing URL
- `POST /api/relay-revoke` -- kicks all remote clients and rotates token

### Revoke All Remote Access
The local console has a red "撤销所有远程访问" button. It calls `POST /api/relay-revoke`, which invokes `RelayClient.revoke_all()`. Flow: daemon sends `{"type":"revoke_all"}` to the relay DO; the DO broadcasts `{"type":"revoked","reason":"daemon_revoked"}` to all clients and closes their sockets with code 4001, then deletes the stored pairing token so the old URL is dead; the daemon then rotates its own token. The remote pair page shows a Chinese "访问已撤销" screen and suppresses auto-reconnect. Use this if a pairing link leaks.

### WebSocket Heartbeat
Both daemon (`relay_client.py`) and mobile client (`pair-page.ts`) send `{"type":"ping","ts":<ms>}` every 30s. The relay DO (`relay-session.ts`) replies `{"type":"pong","ts":<original>}` and never forwards ping/pong to the peer. If no pong arrives within 60s, the sender closes its socket, which triggers reconnect (daemon) or the revoked flag path (client). The DO also tracks `lastSeenAt` per connection via `serializeAttachment`; when its alarm fires, it closes any socket that hasn't sent anything in 90s. Heartbeat frames stay outside any future E2EE envelope so the relay can route them.

### End-to-End Encryption (Relay)
Application traffic between the Mac daemon and each mobile client is E2E encrypted AND the daemon authenticates itself via a long-term ed25519 identity key, so even a compromised Cloudflare Worker cannot successfully MITM. The Worker relay is a dumb pipe — it only sees ciphertext envelopes plus the handshake public keys. Keys never leave the endpoints.

Daemon identity (`identity.py`): on first run the daemon generates an ed25519 keypair at `.identity/daemon.ed25519.key` (raw 32B, 0600 perms, gitignored). Its fingerprint — `base32(SHA256(pub)[:16]).lower()`, 26 chars — is embedded in the pairing URL as a `#fp=…` fragment. Fragments are **never** sent to any server by any browser, so this fingerprint reaches the browser out-of-band from the relay. Losing the key file invalidates all previously-distributed pairing URLs.

Handshake (ECDH P-256, HKDF-SHA256, ed25519-signed):
1. Client opens the WebSocket, generates an ephemeral ECDH P-256 keypair, computes `client_id = SHA256(client_pub)[:16].hex()` (binds the id to the key — a token-holder cannot spoof another client's slot), and sends `{"type":"e2ee_handshake","client_id":"<hex>","pubkey":"<base64 raw uncompressed point>"}`.
2. Daemon (`relay_client.py::_on_handshake`) verifies `client_id == SHA256(client_pub)[:16].hex()` and rejects on mismatch. It generates an ephemeral P-256 keypair, derives a 32-byte AES-GCM key via `HKDF-SHA256(ecdh_shared, info="roboot-relay-e2ee-v1", salt=empty)`, and replies with `{"type":"e2ee_handshake","client_id":"<hex>","pubkey":"<b64 daemon_ephemeral_pub>","id_pubkey":"<b64 ed25519_pub_32B>","sig":"<b64 ed25519_sig>"}`. The signature covers `daemon_pub ‖ client_pub ‖ client_id.encode()` (all fixed lengths so concat is unambiguous).
3. Browser (`pair-page.ts::completeHandshake`) verifies:
   - `base32(SHA256(id_pubkey)[:16]).lower()` matches the `fp` from the URL fragment (catches a swapped daemon identity).
   - `ed25519.verify(id_pubkey, sig, daemon_pub ‖ client_pub ‖ client_id)` succeeds (catches a swapped ephemeral pub with mismatched signature).
   On failure: close WS with code 4003, show error screen, do NOT derive a session key.
4. After both verifications pass, browser runs the same HKDF-SHA256 chain (`deriveBits` → `importKey(HKDF)` → `deriveKey(AES-GCM)`) to arrive at the matching AES key.

Browser requirement: WebCrypto Ed25519 (Chrome 113+, Firefox 129+, Safari 17+).

Encrypted envelope (every app message after handshake):
```
{"type":"encrypted","client_id":"<hex>","iv":"<base64 12B>","ct":"<base64 AES-GCM ciphertext>"}
```
Plaintext is the original JSON message UTF-8-encoded. Fresh 96-bit IV per message (`os.urandom(12)` / `crypto.getRandomValues`). AAD is empty; `client_id` is bound to the pubkey by construction, so a tampered `client_id` in the envelope would look up a cipher that fails GCM authentication.

Relay contract (`relay/src/relay-session.ts`):
- Whitelist of forwardable message types: `e2ee_handshake`, `encrypted`, `ping`, `pong`, `error`. Anything else is dropped on the floor — a compromised client cannot smuggle plaintext through.
- No crypto in the Worker. Auth, rate limiting, and token rotation logic are unchanged.

Key lifecycle:
- One AES key per (daemon, client_id) pair; multiple clients each get an independent key.
- Daemon clears all ciphers on WebSocket (re)connect and on `rotate_token()`; clients clear on WebSocket close/reconnect, forcing a fresh handshake.
- If a daemon-side cipher is missing when an `encrypted` frame arrives, the daemon replies with an unencrypted `{"type":"error","content":"handshake_required"}` and the browser's reconnect path handles rekeying.

Debug: set `DEBUG_E2EE=1` in the daemon's environment (or flip `DEBUG_E2EE = true` in devtools for the browser) to log handshake + ciphertext metadata. Neither logger writes plaintext or key material.

### Streaming Protocol
WebSocket messages from server to frontend:
- `{"type": "thinking"}` -- agent is processing
- `{"type": "delta", "text": "..."}` -- streaming text chunk
- `{"type": "tool_start", "name": "..."}` -- tool execution started
- `{"type": "tool_end", "name": "..."}` -- tool execution ended
- `{"type": "done", "content": "...", "tools_used": N, "sessions": [...]}` -- stream complete
- `{"type": "response", "content": "..."}` -- non-streaming message (welcome)
- `{"type": "error", "content": "..."}` -- error
- `{"type": "tts_request", "req_id": N, "text": "..."}` -- client→daemon: synthesize spoken text (mobile relay path only)
- `{"type": "tts_audio", "req_id": N, "mp3_b64": "..." | "error": "..."}` -- daemon→client: Edge TTS MP3 response
- `{"type": "soul_review", "req_id": H, "origin": "...", "diff": "...", "timeout_s": 30}` -- daemon→clients: requires user approve/reject on a proposed soul.md overwrite
- `{"type": "soul_review_decision", "req_id": H, "approved": bool}` -- client→daemon: user's choice; resolves the waiting review_write future
- `{"type": "tool_approval", "req_id": H, "tool": "...", "args_summary": "...", "danger_reason": "...", "origin": "...", "issued_at": ts, "timeout_s": 30}` -- daemon→clients: agent wants to run a tool flagged as dangerous; user must approve/reject
- `{"type": "tool_approval_decision", "req_id": H, "approved": bool}` -- client→daemon: user's choice; resolves the waiting tool_guard future. (Telegram surface uses inline-keyboard `tool_ok:<req_id>` / `tool_no:<req_id>` callback_data instead of a JSON frame.)

## Configuration

`config.yaml` (gitignored, copy from `config.example.yaml`):
- `providers`: API keys for deepseek, anthropic, glm, etc.
- `default_provider` / `default_model`: which LLM to use
- `daily_budget_usd`: spending cap
- `voice.tts_voice`: Edge TTS voice name (global default; per-user Telegram prefs override via `/voice`)
- `voice.stt.backend`: `mlx_whisper` (default) | `google` | `none`
- `voice.stt.model`: mlx-only, e.g. `whisper-large-v3-mlx` / `whisper-medium-mlx` / `whisper-small-mlx`
- `voice.stt.language`: transcription language hint (default `zh`). Env overrides: `ROBOOT_WHISPER_MODEL`, `ROBOOT_WHISPER_LANGUAGE`.
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
