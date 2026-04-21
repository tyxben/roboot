# Roboot Usage Guide

> 中文版: [USAGE.zh.md](USAGE.zh.md)

For people who just cloned the repo and want to actually use Roboot. This is not a contributor guide — it covers running and living with Roboot, not changing it.

---

## 1. Quick Start

You'll need macOS, Python 3.11+, [iTerm2](https://iterm2.com/), and at least one LLM API key ([DeepSeek](https://platform.deepseek.com) is recommended — cheap and stable).

```bash
git clone https://github.com/tyxben/roboot.git && cd roboot
pip install "arcana-agent[all-providers]>=0.4.0" pyyaml fastapi "uvicorn[standard]" edge-tts iterm2 "qrcode[pil]" cryptography
cp config.example.yaml config.yaml    # then edit and add your API key
python server.py                      # open http://localhost:8765
```

The startup banner prints your local IP, a QR code, and (if enabled) the relay pairing URL. Once you see `🤖 Roboot - Personal AI Agent Hub` the process is up; open the web page and a `Hey，我是 <name>。有什么事？` welcome bubble appears as soon as the WebSocket connects.

---

## 2. Your First Conversation

Open [http://localhost:8765](http://localhost:8765). You get a chat pane with an iTerm2 sessions sidebar on the right (empty at first).

**Channel awareness.** Roboot's system prompt is built fresh on every connect with the current channel (`web` / `cli` / `voice` / `telegram`) and a summary of every running Claude Code session injected in a `## Current context` block. So it knows where you're talking to it from and what windows you have open — no need to repeat yourself. On Telegram it automatically keeps replies shorter; in voice mode it avoids reading out code blocks.

**A typical tool-use turn.** Type:

```
list my Claude Code sessions
```

You'll see the thinking bubble, a streaming reply, a small `1 tool call` badge underneath (that's `list_sessions`), and the right sidebar populating with one card per session. Click any card to read its terminal output and send it commands directly from the browser.

**The `> ` TTS convention.** Everything the model wants *spoken aloud* is prefixed with a `> ` blockquote. Everything else (code, tables, long lists) is on-screen only. When voice output is on, the server's `_extract_spoken_text()` reads just the `> ` lines. On the page those lines still render as normal blockquotes — nothing looks weird. That's what the speaking-style section in `soul.md` is enforcing.

---

## 3. Configuration

```bash
cp config.example.yaml config.yaml
```

`config.yaml` is gitignored, so your keys stay local. The fields:

- `providers` — API keys per LLM. Fill at least one; empty entries and `sk-REPLACE_ME` placeholders are ignored automatically.
- `default_provider` / `default_model` — which LLM to use. Must match a key under `providers`. Defaults to `deepseek` + `deepseek-chat`.
- `daily_budget_usd` — soft daily spend cap in USD, enforced in-process by Arcana's budget tracker. Default 5.
- `voice.tts_voice` — Edge TTS voice name, e.g. `zh-CN-XiaoxiaoNeural`, `en-US-JennyNeural`. Empty = `zh-CN-YunxiNeural`. Full list: `edge-tts --list-voices`.
- `voice.language` — speech-recognition language for CLI `--voice`.
- `name` / `personality` — assistant name and base prompt. Note: if `soul.md` exists (it does by default), its Identity/Personality/Speaking Style sections take precedence. See section 6.
- `telegram.bot_token` — token from @BotFather. Empty = Telegram disabled.
- `telegram.allowed_users` — list of allowed Telegram user IDs (get yours from @userinfobot). **Empty list means anyone can talk to your bot — strongly discouraged.**
- `remote_access.method` — `"none"` / `"official_relay"` / `"custom_relay"`.
- `remote_access.relay.enabled` / `endpoint` — whether to connect to a relay and where. The official relay is `wss://relay.coordbound.com` (free, Cloudflare Workers). To self-host, see `relay/`.

Restart `python server.py` after editing.

---

## 4. Interfaces

### Web console (primary)

```bash
python server.py    # https://localhost:8765 (if certs exist) else http://localhost:8765
```

One-stop frontend: chat, iTerm2 sidebar, camera, voice, relay QR, and the red *撤销所有远程访问* (revoke-all-remote) button.

### CLI

```bash
python run.py            # keyboard only
python run.py --voice    # local mic + macOS `say` TTS
```

Useful for quick one-liners from a terminal without opening a browser.

### Telegram bot

```bash
python -m adapters.telegram_bot
```

Requires `telegram.bot_token` in `config.yaml`; you should also set `allowed_users`. Supports `/start`, `/sessions` (interactive iTerm2 session management), `/screenshot`, `/remote`, `/refresh`. Plain messages go straight to the assistant.

### Remote via relay

Set `remote_access.relay.enabled: true` in `config.yaml` and keep the official `endpoint`. Restart `server.py`: the banner now includes a pairing URL and QR. Scan from your phone and you get the same web console, but traffic goes through the Cloudflare Worker end-to-end encrypted (ECDH P-256 → HKDF → AES-GCM, with the daemon signing its ephemeral key using a long-term ed25519 identity). Pairing tokens rotate every 30 minutes; the red **撤销所有远程访问** button on the local console kicks every paired phone and invalidates the old URL instantly.

---

## 5. Claude Code Integration

Talking to the Claude Code sessions running in your iTerm2 tabs is the main point of Roboot.

**Enable the iTerm2 Python API.** iTerm2 → Settings → General → Magic → check **Enable Python API**. The first connection will prompt you from inside iTerm2 — allow it.

**Four tools.** Ask Roboot in plain language:

- `list_sessions` — "list my Claude Code sessions" → project name, window name, session_id.
- `read_session` — "what's the hair session doing?" → reads the last 100 lines of that session (fuzzy project-name match).
- `send_to_session` — "tell the agent one to keep going" or "hit y on that prompt" → sends text to the matched session.
- `create_claude_session` — "spin up a new Claude Code for this PR" → opens a new iTerm2 tab and starts Claude Code there.

**🔔 Proactive notifier.** `session_watcher.py` polls every session every 5 seconds. Whenever a Claude Code session transitions into a `[y/n]` / "Do you want to proceed" prompt, the server broadcasts a notify frame (with text starting `🔔 Session <project>: <prompt line>`) to every connected web console, including mobile clients via the relay. The bell is your signal that some Claude Code is waiting on you.

---

## 6. Personality & Memory

`soul.md` is Roboot's **self-modifiable identity file**. It's checked into the repo but also rewritable at runtime. Five sections:

- `## Identity` — name, voice, role.
- `## Personality` — character traits.
- `## Speaking Style` — how it talks.
- `## About User` — accumulated facts about you.
- `## Notes` — notes it writes to itself.
- `## 自我反馈` — its own reflections on how it's been doing.

Every new chat session rebuilds the system prompt from `soul.md` on the fly (`tools/soul.py::build_personality`). The assistant can call three tools to edit it: `update_self` (name / voice / personality / style), `remember_user` (adds a dated bullet under About User), `add_note` (notes to itself). Just say "call me Ty from now on" or "rename yourself Ava" or "switch to the Xiaoxiao voice" — it does it.

**Automatic distillation.** After every 20 turns, `memory.py` fires two background distillations in parallel: one condenses what was newly learned about you into `## About User`, the other writes a pointed self-critique (max ~60 Chinese chars) into `## 自我反馈`. Both prompts require the model to output the literal word `NOTHING` when nothing is worth recording, so idle chatter doesn't bloat the file.

**Hand editing.** `soul.md` is plain Markdown — edit freely. If the assistant records something wrong or weird ("user enjoys being photographed at 3am"), just delete the line. Every write snapshots the prior version to `.soul/history/<timestamp>.md` so nothing is lost.

---

## 7. Auto-upgrade

Off by default. Enable with an env var:

```bash
ROBOOT_AUTO_UPGRADE=1 python server.py
```

When on, every hour: `git fetch origin main` → if there are new commits and the working tree is clean → `git pull --ff-only` → run `python -m pytest tests/ -x` (60s budget) → on success, write `.upgrade_pending`, broadcast `⬆️ 升级到 <sha>，重启中…` to every connected console, and `os.execv` to re-exec. Any failure (network, test timeout, test failure) rolls back to the pre-upgrade SHA and keeps running. If a chat turn is streaming, the upgrade defers to the next tick.

Set `ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG=1` alongside `ROBOOT_AUTO_UPGRADE=1` to additionally require that `origin/main`'s HEAD be pointed to by a `v*` tag that `git verify-tag` accepts (GPG or SSH signing, per your local config). Ticks where no verified tag is present are silently skipped — so release cuts must push a signed tag (`git tag -s vX.Y.Z && git push --tags`) for the loop to move.

**Turn it on for:** long-lived deployments where you don't actively touch the code.
**Leave it off for:** active development, repos with uncommitted local edits, or any setup where you don't want `main` to auto-apply.

---

## 8. Troubleshooting

**`iTerm2 not connected` / `list_sessions` always empty.** Confirm iTerm2 → Settings → General → Magic → **Enable Python API** is checked, and that you accepted iTerm2's authorization popup the first time. Restart `server.py`.

**Mobile relay pair page hangs / TLS error.** The LAN console uses a self-signed cert. On iOS Safari the first visit will warn about it — tap "Show Details → Visit this Website" to proceed; full walkthrough in [docs/ssl-trust-guide.md](ssl-trust-guide.md). The relay path uses Cloudflare's real cert so there's nothing to trust, but if it hangs: check that `python server.py`'s banner shows `✅ Relay started` and that `remote_access.relay.enabled: true`.

**Telegram bot runs but no one can talk to it.** Check `telegram.allowed_users` in `config.yaml`. If it's **empty**, the bot is open to everyone in principle — but they still have to `/start` first, and if nothing is coming through you may have locked *yourself* out. Add your user ID (from @userinfobot) to the list.

**The red revoke button is greyed out / no relay QR appears.** The relay isn't enabled. Set `remote_access.relay.enabled: true` in `config.yaml` and confirm `endpoint` (official: `wss://relay.coordbound.com`), then restart.

**Startup banner shows `⚠️ SSL/TLS: DISABLED` / camera and voice don't work in the browser.** Browsers only allow `getUserMedia` over HTTPS. Drop a `cert.pem` + `key.pem` into `certs/` (the repo ships a self-signed pair; if it doesn't fit your hostname, regenerate with `openssl req -x509 -newkey rsa:4096 -nodes -keyout certs/key.pem -out certs/cert.pem -days 365 -subj "/CN=localhost"`) and restart.
