# Security

Honest accounting of what Roboot protects, where the gaps are, and how to report a vulnerability. Aimed at someone deciding whether to run it. If you'll expose Roboot beyond localhost, read it all.

## 1. Threat model

**Local machine (the Mac).** We assume the attacker does *not* have read access to the user account running the daemon. If they do, the game is over: `config.yaml` (API keys, Telegram token), `.identity/daemon.ed25519.key`, `.chat_history.db`, and `soul.md` sit on disk in plaintext (§2), and the agent inherits user shell permissions via `tools/shell.py`. No in-process defense against a local attacker.

**LAN (same Wi-Fi).** Passive observers see TLS *if* you ran `python -m tools.generate_cert`; otherwise WebSocket is plaintext. The cert is self-signed, no pinning. An active Wi-Fi attacker who gets the user to accept a MITM cert reads and injects all traffic. There is **no auth** on `/ws`, `/api/sessions/*`, `/api/tts`, or `/api/relay-revoke` — anyone who reaches `0.0.0.0:8765` and trusts the cert has full agent access.

**Cloudflare Worker relay (assume fully compromised).** The entire point of the E2EE design. The Worker can passively read every frame, actively drop/reorder/replay frames, and serve arbitrary JavaScript on `/pair/<id>`. It **cannot** decrypt application traffic or successfully MITM a new pairing: the AES-256-GCM key never leaves the endpoints, and the daemon's ed25519 identity is pinned via a URL-fragment fingerprint the Worker never sees. But: a malicious Worker can replace the pair page's JS. The E2EE guarantee therefore holds *only if* the user verifies the identity fingerprint on a known-good channel before first use. If the first JS you ever load is hostile, E2EE is bypassed from the inside.

**Mobile browser.** Assumed honest. A compromised device holds the plaintext key and DOM. Out of scope.

## 2. What's encrypted, what isn't

| Surface | Algorithm | Gap |
| --- | --- | --- |
| LAN WebSocket (`/ws`) | TLS 1.2+ self-signed cert if present, else plaintext | No pinning (TOFU); no auth layer |
| Relay handshake (`e2ee_handshake`) | Plaintext over Worker TLS; ed25519-signed by daemon | Relay sees handshake pubkeys (metadata only) |
| Relay app frames (`encrypted`) | AES-256-GCM; key = HKDF-SHA256 of ECDH P-256 shared secret | No sequence counter; relay could replay a frame, but GCM auth will reject reused-IV within a key |
| Relay heartbeat (`ping`/`pong`) | Plaintext by design (Worker must route without keys) | Leaks liveness only |
| `.chat_history.db` | Plaintext SQLite | At-rest encryption planned |
| `soul.md` | Plaintext Markdown, committed to git | Agent writes it with no review gate |
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

Mitigations: `.soul/history/` keeps timestamped snapshots for hand-rollback; `soul.md` is committed to git, so `git diff` surfaces unexpected changes; the user can edit or delete any section.

Open gap: **no review gate on self-writes.** The soul-write tool call is never surfaced to the user for approval; it just runs. A diff-preview-before-save path is not implemented.

## 7. What to do if your pairing URL leaks

Open the local web console, press the red **撤销所有远程访问** button, or `POST /api/relay-revoke`. In one step this: (a) tells the Worker's Durable Object to broadcast `{"type":"revoked","reason":"daemon_revoked"}` to every connected client and close their sockets with code 4001; (b) deletes the stored pairing token on the relay so no new client can redeem the old URL; (c) rotates the daemon's `session_id` and token locally, killing the old WebSocket so the reconnect loop picks up fresh credentials. Old URL dead end-to-end; old ECDH keys wiped. Issue a new URL by rescanning the fresh QR.

Restarting the daemon is *weaker*: it rotates ephemeral ECDH state and the token, but since the ed25519 identity key is stable across restarts, the `#fp=` in the leaked URL still resolves. Use the revoke button.

## 8. Known gaps & roadmap

- `.chat_history.db` has no at-rest encryption (planned: argon2id + XChaCha20-Poly1305).
- LAN WebSocket uses self-signed TLS with trust-on-first-use; no cert pinning (planned via PWA service worker).
- **Local HTTP API has no authentication.** Anything reachable at `http(s)://<mac>:8765` on the LAN can hit `/ws`, `/api/sessions/*`, `/api/tts`, `/api/relay-revoke`. Guarded only by network reach + TLS trust.
- Signed release tags are opt-in: set `ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG=1` alongside `ROBOOT_AUTO_UPGRADE=1` and the loop will only pull when `origin/main` is at a commit pointed to by a `v*` tag that `git verify-tag` accepts. For this to actually gate anything, release cuts must run `git tag -s vX.Y.Z` (GPG) or the SSH-signing equivalent and `git push --tags`; otherwise no tag points at HEAD of `main` and every tick is a no-op. Without the env var, the upgrade loop still trusts the git remote wholesale.
- No review gate on `soul.md` self-writes.
- No application-layer replay counter on E2EE frames. AES-GCM random IV makes plaintext replay unproductive, but no sequence numbers are tracked.
- Losing `.identity/daemon.ed25519.key` is permanent — every old pairing URL's `#fp=` fails forever. Back it up.
- `.faces/faces.json` stores biometric face encodings in plaintext.
- Secrets on disk (`config.yaml`, Telegram bot token) are plaintext.
- No sandboxing for `shell` / iTerm2 tools: a paired remote client can run arbitrary shell commands with user privileges.
- Prompt injection via a manipulated Claude Code session tail reaches the agent through both the 20-line watcher scan and the `read_session` tool.

Issues and PRs that move any of these forward are welcome.

## 9. Reporting a vulnerability

Email: **felixttysa@gmail.com**.

Please do **not** open public GitHub issues for security problems. There is no bounty — this is a personal project. I acknowledge reports and coordinate a fix / disclosure timeline in good faith.
