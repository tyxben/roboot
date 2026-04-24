_[中文版](CHANGELOG.zh.md)_

# Changelog

All notable changes to Roboot. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [SemVer](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **FileVault-off warning banner** (`filevault_status.py`, `/api/filevault-status`) — local console polls macOS `fdesetup status` on load and shows a red sticky banner if the boot volume isn't encrypted. Roboot's entire at-rest security model (plaintext `config.yaml` with API keys, `.identity/daemon.ed25519.key`, `soul.md`, `.chat_history.db`) assumes FileVault is on; the banner surfaces the assumption instead of leaving it silent. Dismissible per tab via `sessionStorage`.
- **"擦除所有聊天" button** (`chat_store.wipe_all`, `POST /api/chat-history-wipe`) — one-click hygiene control in the Network panel that drops every session + message row and VACUUMs the DB so deleted pages aren't recoverable from the file. Lightweight alternative to at-rest DB encryption (which was dropped as theater — see `SECURITY.md`).
- **Soul review gate** (`soul_review.py`) — every write to `soul.md` from `update_self` / `remember_user` / `add_note` / the self-feedback distiller goes through a review gate. Mode selected by `ROBOOT_SOUL_REVIEW`: `off` (default, backwards compatible), `log` (writes proceed, diff audited to `.soul/pending/`), `confirm` (modal on local console + mobile pair-page, user approves each write; timeout or oversize diff → rejected). Blocks prompt-injected agents from silently persisting hostile content.
- **`CONTRIBUTING.md`** + `CONTRIBUTING.zh.md`, `.github/ISSUE_TEMPLATE/` (bug_report / feature_request / config.yml), `.github/PULL_REQUEST_TEMPLATE.md` — standard open-source housekeeping for the v0.4.0-and-later flow.
- **Mobile TTS proxy** — relay pair-page now fetches MP3 from the daemon's Edge TTS over the encrypted WS (`tts_request` / `tts_audio` frames) so mobile JARVIS matches local console voice; falls back to browser `speechSynthesis` if the daemon is unreachable. Shared helper `tts_synth.synthesize_spoken()` keeps the two paths from drifting.

## [0.4.0] - 2026-04-22

First release since the repo went public. Large feature pass on Telegram voice I/O, LAN security, self-upgrade, and conversation memory.

### Added
- **Telegram voice I/O** — mlx-whisper STT + parallel Edge TTS, synthesized to native OGG/Opus voice bubbles (`adapters/telegram_bot.py`, `adapters/tts_streamer.py`).
- **Pluggable STT backends** — `mlx_whisper` (default, Apple Silicon), `google` (Web Speech), `none`; selected via `voice.stt.backend` (`adapters/stt/`).
- **Per-user Telegram voice preferences** — `/voice` picker or `switch_tts_voice` agent tool; persisted to `.voice_prefs/prefs.json`.
- **Telegram `/help`** — discoverable command list.
- **One-command installer** — `scripts/setup.sh` (prefers `uv` when available, handles ffmpeg + Whisper model prewarm).
- **LAN API bearer-token auth** — `Authorization: Bearer <token>` for `/api/*`, `Sec-WebSocket-Protocol: bearer.<token>` for `/ws`; token auto-generated to `.auth/lan_token` (0600), embedded in the LAN QR.
- **Self-signed TLS cert tool** — `tools/generate_cert.py` (ECDSA P-256 via `cryptography`).
- **In-process self-upgrade loop** — opt-in via `ROBOOT_AUTO_UPGRADE=1`; hourly fetch + smoke-test + re-exec + rollback. Signed-tag gate via `ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG=1`.
- **Conversation memory** — Layer A replay on `resume_session_id` (daemon side); Layer B periodic distillation into `soul.md`.
- **Self-review distillation** — every 20 turns the agent writes concrete feedback into `soul.md ## 自我反馈`.
- **Proactive session-waiting notifier** — `session_watcher.py` broadcasts `type:"notify"` frames when a Claude Code session transitions idle → waiting.
- **Frontend notify handlers** — top-right toast + history drawer on `static/console.html`; bottom-pinned toast + haptic + history on mobile `pair-page.ts`.
- **Channel + session-list context injection** into the agent system prompt (`tools/soul.py::build_personality`).
- **GitHub Actions CI** — pytest on push + PR.
- **User-facing docs** — `docs/USAGE.{zh,}.md`, `docs/REMOTE_VS_LOCAL.md`, `README.zh.md`; expanded `SECURITY.md` with threat model + E2EE trust chain.
- **`.allowed_signers`** — committed SSH signing principals so `git verify-tag` works out-of-the-box.

### Changed
- STT model is prewarmed at startup (faster first transcription).
- `voice.tts_voice` config value now honored end-to-end.
- README simplified; install quickstart on top.
- `uv.lock` committed for reproducible installs.

### Security
- LAN API now requires an auto-generated bearer token (fail-closed).
- `session_watcher.py` hardening: `TAIL_LINES` 20 → 10; `_sanitize_prompt_line()` strips ANSI, HTML-escapes, truncates to 200 chars (prompt-injection mitigation for notification surfaces).
- Self-upgrade can require a verified signed tag (`ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG=1`) before pulling.
- This release is the first one with a verifiable signed tag (SSH signature; verify locally with `git verify-tag v0.4.0` once `.allowed_signers` is in place).

### Fixed
- Duplicate `_active_ws_clients` definition from a bad merge.
- Telegram transcript echo dropped (user hears TTS instead of seeing their own transcription echoed back).
- CI: `setuptools` package discovery restricted so test runs don't pick up unintended modules.

---

## [0.3.0] - 2026-04-19

### Security
- **Signed relay handshake** — daemon holds a long-term ed25519 identity at `.identity/daemon.ed25519.key`; fingerprint embedded in the pairing URL fragment (`#fp=…`, never sent to the relay). Browser verifies the daemon signature over `daemon_pub ‖ client_pub ‖ client_id` before deriving a session key. Closes the v0.2.0 anonymous-ECDH MITM gap.
- **client_id binding** — `client_id = SHA256(client_ephemeral_pub)[:16].hex`; token-holders can no longer spoof another client's slot.

### Breaking
- Wire protocol: daemon + Cloudflare Worker must redeploy together.
- Browser requirement: WebCrypto Ed25519 (Chrome 113+, Safari 17+, Firefox 129+).

---

## [0.2.0] - 2026-04-18

### Added
- **E2EE for the relay path** — ECDH P-256 + HKDF-SHA256 + AES-GCM. Cloudflare Worker only sees ciphertext envelopes.
- Relay Durable Object: whitelist of forwardable message types (`e2ee_handshake`, `encrypted`, `ping`, `pong`, `error`).
- **Revoke-all button** — kicks all remote clients and rotates the pairing token.
- **WebSocket heartbeat** — 30s ping / 60s timeout; daemon-side alarm closes dead connections after 90s.
- Incremental terminal-history fetch (local console + relay).
- ANSI color rendering in the web console terminal view.
- Lazy-load session list + confirm-bar + concurrent relay handlers.
- SQLite chat-history persistence (`chat_store`).
- `soul.md` writes: snapshot before overwrite + date-stamp on appends.
- Minimal pytest suite (32 tests).
- LICENSE + README + SECURITY.md + `config.example.yaml` (open-source documentation pass).

### Fixed
- Stopped auto-rotating the relay token on every reconnect.
- Chat send button no longer gets stuck disabled after tab switches.
- Telegram bot: send-command and session-view bugs.

### Infrastructure
- Relay Durable Objects pinned to APAC to cut transpacific round-trips.

---

## [0.1.0] - 2026-04-18

Initial public preview.

### Added
- Roboot personal AI agent hub skeleton.
- Web console + iTerm2 bridge + Chainlit integration.
- Telegram bot + browser voice input.
- Streaming responses + sentence-by-sentence JARVIS TTS.
- Edge TTS with throttled rendering.
- **Soul system** — self-modifiable identity via `soul.md`.
- Relay architecture + PWA mobile access + face recognition baseline.

[Unreleased]: https://github.com/tyxben/roboot/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/tyxben/roboot/compare/v0.3.0...v0.4.0
[0.3.0]: https://github.com/tyxben/roboot/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/tyxben/roboot/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/tyxben/roboot/releases/tag/v0.1.0
