# Roboot

![CI](https://github.com/tyxben/roboot/actions/workflows/test.yml/badge.svg)

A personal AI agent hub that lives on your Mac: chat, voice, camera, and hands-on control of your iTerm2 Claude Code sessions -- from a laptop, a phone on the same Wi-Fi, or anywhere in the world via an end-to-end encrypted relay.

Built for people who already run a lot of Claude Code sessions and want one place to watch and talk to them.

![demo](docs/demo.gif)

## Requirements

- **macOS** -- iTerm2 integration is macOS-only
- **Python 3.11+**
- **[iTerm2](https://iterm2.com/)** with the Python API enabled: *iTerm2 → Settings → General → Magic → Enable Python API*
- At least one LLM API key (DeepSeek recommended -- cheap and stable with tool calling)

## Quickstart

```bash
git clone https://github.com/tyxben/roboot.git && cd roboot
pip install "arcana-agent[all-providers]>=0.4.0" pyyaml fastapi "uvicorn[standard]" edge-tts iterm2 "qrcode[pil]" cryptography
cp config.example.yaml config.yaml    # then edit and add your API key
python server.py                      # open http://localhost:8765
```

That's it. The welcome message will appear when the WebSocket connects.

### Other entry points

```bash
python run.py                     # Keyboard-only CLI
python run.py --voice             # Local mic + macOS `say` TTS
python -m adapters.telegram_bot   # Telegram bot (requires telegram.bot_token)
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

tools/
├── shell.py                     <- Terminal command execution
├── claude_code.py               <- iTerm2 session list/read/send/create
├── vision.py                    <- Camera + screenshot + face recognition
├── face_db.py                   <- Face encoding storage (.faces/)
└── soul.py                      <- Self-modification + user memory

adapters/
├── telegram_bot.py              <- Remote control via Telegram
├── voice.py                     <- Local mic STT + macOS TTS
└── keyboard.py                  <- Terminal text input

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
