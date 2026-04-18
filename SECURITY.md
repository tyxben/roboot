# Security

This document is honest about what Roboot protects, where the gaps are, and how to report a vulnerability. If you intend to expose Roboot beyond localhost, read it all.

## Reporting a vulnerability

Email: **felixttysa@gmail.com**

Please do **not** open public GitHub issues for security problems. There is no bounty -- this is a personal project. I will acknowledge reports and coordinate a fix / disclosure timeline in good faith.

## What is protected

- **Relay traffic (daemon ↔ browser) is end-to-end encrypted.** Each pairing uses an ephemeral ECDH P-256 handshake, HKDF-SHA256 to derive a 32-byte AES-GCM key (`info="roboot-relay-e2ee-v1"`), and a fresh 96-bit random IV per message. The Cloudflare Worker only ever sees `{"type":"encrypted","client_id":"…","iv":"…","ct":"…"}` envelopes plus the handshake public keys. Plaintext never touches the relay.
- **Authenticated daemon identity (v0.3.0).** The daemon holds a long-term ed25519 keypair at `.identity/daemon.ed25519.key`. Its fingerprint (26-char base32 of SHA256) travels in the pairing URL as a `#fp=…` fragment — fragments are never sent to any server by any browser, so this value reaches the pair page out-of-band from the relay. During handshake the daemon signs `(daemon_pub ‖ client_pub ‖ client_id)` with its identity key; the browser rejects the connection if either the fingerprint or the signature fails to verify. A compromised relay that tries to substitute its own ECDH keypair cannot forge a matching signature. Browser requirement: WebCrypto Ed25519 (Chrome 113+, Firefox 129+, Safari 17+).
- **`client_id` bound to pubkey (v0.3.0).** `client_id = SHA256(client_ephemeral_pub)[:16].hex()`. The daemon rejects handshakes whose declared id doesn't match. A token-holder cannot pair as an arbitrary `client_id` to kick another user's cipher slot.
- **Relay message whitelist.** The Durable Object forwards only `e2ee_handshake`, `encrypted`, `ping`, `pong`, and `error` frames. A compromised client cannot smuggle plaintext through.
- **Key isolation per client.** Each `(daemon, client_id)` pair gets its own AES key. Keys are cleared on WebSocket reconnect and on token rotation, forcing a fresh handshake.
- **Pairing token rotation.** Relay pairing tokens are 256-bit (`secrets.token_hex(32)`), expire after 30 minutes, and can be regenerated on demand (`POST /api/relay-refresh`). The relay compares tokens in constant time.
- **Instant revocation.** The red *撤销所有远程访问* button in the local console calls `POST /api/relay-revoke`, which kicks all connected relay clients with code 4001, deletes the stored pairing token, and rotates daemon credentials in one step. Use this if a pairing link leaks.
- **Daemon restart invalidates pairings.** A restarted daemon generates a new token and new ECDH keys; every existing pair page is dead.
- **Relay rate limiting.** The Worker caps new sessions at 10/hour per source IP.
- **Heartbeat timeouts.** Idle WebSocket connections are closed by the Durable Object after ~90 seconds of silence; both peers ping every 30s and disconnect after 60s without a pong.
- **Telegram access control.** `telegram.allowed_users` is a strict allowlist of Telegram user IDs. Unknown users are rejected before any tool call.

## What is NOT protected (known gaps)

Be specific about these before exposing Roboot to the public internet.

- **QR / pairing-URL delivery is trust-on-first-use.** The daemon identity fingerprint in `#fp=…` is only as trustworthy as the channel that delivered the URL to the browser. A physical attacker who swaps the QR you're about to scan, or a MITM who can modify the local console's response, can substitute their own fingerprint. Scan the QR on the same device that's paired with your daemon (local console) and you're fine.
- **Local LAN uses self-signed TLS with trust-on-first-use.** An attacker on the same Wi-Fi who can get a user to accept a MITM certificate can read chat traffic. There is no cert pinning.
- **No application-layer replay counter.** AES-GCM's random IV makes replayed ciphertext fail to produce useful decrypted output, but a malicious relay could in principle re-deliver an old frame; the application does not track message sequence numbers.
- **Losing `.identity/daemon.ed25519.key` is permanent.** If you delete it, the daemon generates a new identity on next start; every old pairing URL's `#fp=` will no longer match, and every browser that ever paired will hard-reject until you rescan. Back it up if this matters to you.
- **Secrets on disk are plaintext.** `config.yaml` (LLM API keys, Telegram bot token), `.faces/faces.json` (face encodings), `.chat_history.db` (full chat log), and `.soul/history/` (soul snapshots) are all unencrypted. Anyone with filesystem read access to your Mac account can read them.
- **No sandboxing for shell/iTerm2 tools.** The agent has the same permissions as the user running the daemon. An attacker who reaches a working chat session can run arbitrary shell commands. Don't ship a working pair page to anyone you wouldn't hand a terminal.
- **Soul self-modification is not constrained.** The assistant can rewrite `soul.md` (including its own safety prompts) based on conversation. Prompt injection in incoming messages can meaningfully alter future behavior. Snapshots in `.soul/history/` let you roll back.

## Planned improvements

Rough order of priority. No promised dates.

- **TLS pinning for local LAN.** Pin the self-signed cert via PWA service worker or companion app, removing the TOFU window.
- **At-rest encryption.** Encrypt `.chat_history.db` and `config.yaml` secrets with a passphrase-derived key (argon2id + XChaCha20-Poly1305).
- **Tool-level confirmation for destructive actions.** Gate `shell` and Claude Code "send to session" calls on an additional remote-origin approval step when initiated from relay/Telegram.

Issues and PRs that move any of these forward are welcome.
