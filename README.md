# Roboot

![CI](https://github.com/tyxben/roboot/actions/workflows/test.yml/badge.svg)

> 中文版: [README.zh.md](README.zh.md)

A personal AI agent hub that lives on your Mac: chat, voice, camera, and hands-on control of your iTerm2 Claude Code sessions -- from a laptop, a phone on the same Wi-Fi, or anywhere in the world via an end-to-end encrypted relay.

Built for people who already run a lot of Claude Code sessions and want one place to watch and talk to them.

![demo](docs/demo.gif)

## Requirements

- **macOS** -- iTerm2 integration is macOS-only
- **Python 3.11+**
- **[iTerm2](https://iterm2.com/)** with the Python API enabled: *iTerm2 → Settings → General → Magic → Enable Python API*
- At least one LLM API key (DeepSeek recommended -- cheap and stable with tool calling)

## Quickstart

One command for most people:

```bash
git clone https://github.com/tyxben/roboot.git && cd roboot
./scripts/setup.sh                    # installs deps + ffmpeg + prewarms Whisper
# edit config.yaml: add providers.deepseek key, optional telegram.bot_token
python server.py                      # open http://localhost:8765
```

The script checks your Python version, installs the `telegram` extras (bot + voice I/O), `brew install`s ffmpeg if missing, copies `config.example.yaml` → `config.yaml`, and pre-caches the Whisper model so the first voice message is instant. It's idempotent — safe to re-run. Flags: `--with=core|telegram|voice|vision|all`, `--no-prewarm`.

If [`uv`](https://docs.astral.sh/uv/) is on your PATH the script uses it automatically (much faster resolver, handles numpy-2-vs-numba collisions better than pip). To install uv first:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Prefer to install manually:

```bash
pip install -e .                      # core: web console + LAN + relay
cp config.example.yaml config.yaml    # then edit and add your API key
python server.py                      # open http://localhost:8765
```

That's it. The welcome message will appear when the WebSocket connects.

### Optional extras

`pyproject.toml` defines four extras you can mix and match — pull them with `pip install -e '.[<name>,<name>]'`:

- `telegram` — Telegram bot with voice input (mlx-whisper) + voice output (Edge TTS → OGG/Opus)
- `voice` — local mic STT + macOS `say` TTS for CLI `--voice` mode (needs `brew install portaudio` first)
- `vision` — camera + face recognition (`look` tool, `enroll_face` tool)
- `desktop` — pywebview standalone app wrapper
- `all` — everything above in one shot

### Enabling Telegram voice

```bash
pip install -e '.[telegram]'           # pulls mlx-whisper + SpeechRecognition
brew install ffmpeg                    # encodes voice replies to OGG/Opus
python -m adapters.stt prewarm         # pre-cache the ASR model (~3 GB, one-time)
python -m adapters.telegram_bot        # start the bot
```

The prewarm step is optional but recommended — without it, the **first** Telegram voice message you send waits ~6 minutes on the model download. Run it once during setup and all future voice messages feel instant.

Inside Telegram you can:
- Send voice → the bot transcribes with Whisper (`~96%` Chinese accuracy on `large-v3`), the agent replies, and you hear the reply back as a voice bubble in ~3–4s.
- `/voice` — pick from 10 curated voices (male/female Mandarin + two dialects + English).
- Just say "换成女声" / "screenshot please" — the agent owns tools like `switch_tts_voice`, `screenshot`, `list_sessions`, `shell`, so slash commands are optional.

### Other entry points

```bash
python run.py                     # Keyboard-only CLI
python run.py --voice             # Local mic + macOS `say` TTS (needs `.[voice]`)
chainlit run chainlit_app.py -w   # Alternative Chainlit UI
```

## Remote access

Three ways to reach your Roboot from off-device. See [SECURITY.md](SECURITY.md) for the threat model before exposing any of them.

- **LAN (zero-config)** -- the server binds `0.0.0.0:8765`; a QR code is printed at startup. Scan it from a phone on the same Wi-Fi. Uses a self-signed TLS cert with trust-on-first-use.
- **Telegram bot** -- set `telegram.bot_token` in `config.yaml`, run `python -m adapters.telegram_bot`. Gate access with `telegram.allowed_users`.
- **Relay** -- a Cloudflare Worker forwards WebSocket traffic between the daemon and a browser pair page. Traffic is end-to-end encrypted (ECDH P-256 → HKDF → AES-GCM); the relay only sees ciphertext envelopes. Pairing tokens rotate every 30 minutes and can be revoked instantly from the local console.

## Architecture

```
server.py (FastAPI)              <- Main entry point
├── WebSocket /ws                <- Streaming chat (LLM_CHUNK events)
├── REST /api/sessions/*         <- Direct iTerm2 session control
├── REST /api/tts                <- Edge TTS (text -> mp3)
├── REST /api/relay-*            <- Relay status / refresh / revoke / QR
├── REST /api/network-info       <- Local IP addresses + QR
└── Static /static/console.html  <- Unified web console

relay_client.py                  <- Connects to the Cloudflare Worker relay
iterm_bridge.py                  <- Persistent iTerm2 Python API connection
soul.md                          <- Self-modifiable assistant identity
config.yaml                      <- API keys + provider config (gitignored)

text_utils.py                    <- Shared helpers (extract_spoken_text, …)

tools/
├── shell.py                     <- Terminal command execution
├── claude_code.py               <- iTerm2 session list/read/send/create
├── vision.py                    <- Camera + screenshot + face recognition
├── face_db.py                   <- Face encoding storage (.faces/)
├── soul.py                      <- Self-modification + user memory
└── voice_switch.py              <- Agent tool: change Telegram TTS voice

adapters/
├── telegram_bot.py              <- Remote control via Telegram (voice I/O)
├── voice.py                     <- Local mic STT + macOS TTS (CLI --voice)
├── voice_prefs.py               <- Per-Telegram-user TTS voice store
├── tts_streamer.py              <- Edge TTS → parallel OGG/Opus synthesis
├── keyboard.py                  <- Terminal text input
└── stt/                         <- Pluggable speech-to-text backends
    ├── mlx.py                   <- mlx-whisper (Apple Silicon, default)
    ├── google.py                <- speech_recognition → Google Web Speech
    └── noop.py                  <- backend: none

relay/                           <- Cloudflare Worker relay
├── src/index.ts                 <- Worker entry, routing, rate limiting
├── src/relay-session.ts         <- Durable Object: daemon↔client session mgmt
├── src/pair-page.ts             <- Browser pairing page
└── wrangler.toml                <- Cloudflare deployment config
```

Deeper architecture notes (agent framework, TTS conventions, soul system, E2EE handshake, streaming protocol) live in [CLAUDE.md](CLAUDE.md).

## Adding a tool

1. Create `tools/my_tool.py`.
2. Decorate with `@arcana.tool(when_to_use=..., what_to_expect=...)`.
3. Import it and add it to `ALL_TOOLS` in `server.py`.

Arcana handles registration; no other wiring is needed. See the "Adding a New Tool" section in [CLAUDE.md](CLAUDE.md) for conventions.

## Configuration

Every option is documented inline in [`config.example.yaml`](config.example.yaml). The assistant can also rewrite parts of its own identity by editing `soul.md` through the `soul` tool.

## Documentation

- [docs/USAGE.md](docs/USAGE.md) -- end-user guide: quickstart, config, interfaces, Claude Code integration, memory, auto-upgrade, troubleshooting ([中文版](docs/USAGE.zh.md))
- [docs/REMOTE_VS_LOCAL.md](docs/REMOTE_VS_LOCAL.md) -- capability matrix for local / LAN / Telegram / relay, plus a comparison with Claude Code's built-in remote (bilingual in one file)
- [SECURITY.md](SECURITY.md) -- threat model, E2EE trust chain, new-feature risks, pairing-leak recovery
- [CHANGELOG.md](CHANGELOG.md) -- release notes ([中文版](CHANGELOG.zh.md))
- [CONTRIBUTING.md](CONTRIBUTING.md) -- scope, dev setup, PR workflow ([中文版](CONTRIBUTING.zh.md))
- [CLAUDE.md](CLAUDE.md) -- contributor notes: architecture, streaming protocol, soul system, adding tools

## Security

If you plan to expose Roboot beyond localhost, read [SECURITY.md](SECURITY.md) first. It lists what is and isn't protected, known gaps, and how to report vulnerabilities.

## License

[MIT](LICENSE) -- Copyright (c) 2026 tyxben.

## Credits

- [Arcana](https://github.com/tyxben/arcana) -- the agent framework
- [DeepSeek](https://platform.deepseek.com) -- default LLM provider
- [iTerm2](https://iterm2.com/) Python API -- terminal integration
- [Cloudflare Workers](https://workers.cloudflare.com/) + Durable Objects -- relay infrastructure
- [Edge TTS](https://github.com/rany2/edge-tts) -- neural voice synthesis
