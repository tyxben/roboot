# Security

This document is honest about what Roboot protects, where the gaps are, and how to report a vulnerability. If you intend to expose Roboot beyond localhost, read it all.

## Reporting a vulnerability

Email: **felixttysa@gmail.com**

Please do **not** open public GitHub issues for security problems. There is no bounty -- this is a personal project. I will acknowledge reports and coordinate a fix / disclosure timeline in good faith.

## What is protected

- **Relay traffic (daemon ↔ browser) is end-to-end encrypted.** Each pairing uses an ephemeral ECDH P-256 handshake, HKDF-SHA256 to derive a 32-byte AES-GCM key (`info="roboot-relay-e2ee-v1"`), and a fresh 96-bit random IV per message. The Cloudflare Worker only ever sees `{"type":"encrypted","client_id":"…","iv":"…","ct":"…"}` envelopes plus the handshake public keys. Plaintext never touches the relay.
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

- **Anonymous relay handshake.** The ECDH handshake is unauthenticated. If the Cloudflare Worker code, Cloudflare account, or TLS endpoint is compromised, an active man-in-the-middle between daemon and browser is feasible: the attacker terminates both handshakes, sees all plaintext, and re-encrypts. The browser does not currently verify the daemon's identity (no fingerprint, no signed handshake).
- **Local LAN uses self-signed TLS with trust-on-first-use.** An attacker on the same Wi-Fi who can get a user to accept a MITM certificate can read chat traffic. There is no cert pinning.
- **No application-layer replay counter.** AES-GCM's random IV makes replayed ciphertext fail to produce useful decrypted output, but a malicious relay could in principle re-deliver an old frame; the application does not track message sequence numbers.
- **Self-declared `client_id`.** The `client_id` in the E2EE envelope is not cryptographically bound to the client's ephemeral key. A holder of a valid pairing token could attempt to disrupt another client's session; the damage is limited to denial-of-service within that pairing.
- **Secrets on disk are plaintext.** `config.yaml` (LLM API keys, Telegram bot token), `.faces/faces.json` (face encodings), `.chat_history.db` (full chat log), and `.soul/history/` (soul snapshots) are all unencrypted. Anyone with filesystem read access to your Mac account can read them.
- **No sandboxing for shell/iTerm2 tools.** The agent has the same permissions as the user running the daemon. An attacker who reaches a working chat session can run arbitrary shell commands. Don't ship a working pair page to anyone you wouldn't hand a terminal.
- **Soul self-modification is not constrained.** The assistant can rewrite `soul.md` (including its own safety prompts) based on conversation. Prompt injection in incoming messages can meaningfully alter future behavior. Snapshots in `.soul/history/` let you roll back.

## Planned improvements

Rough order of priority. No promised dates.

- **Signed handshake.** Give the daemon a persistent ed25519 identity key and require the browser to verify a fingerprint on first pair (short hash shown by the local console, typed/scanned into the pair page). Closes the anonymous-MITM gap.
- **Bind `client_id` to the key.** Derive `client_id = SHA256(client_ephemeral_pubkey)` so a peer cannot impersonate another session holder.
- **TLS pinning for local LAN.** Pin the self-signed cert via PWA service worker or companion app, removing the TOFU window.
- **At-rest encryption.** Encrypt `.chat_history.db` and `config.yaml` secrets with a passphrase-derived key (argon2id + XChaCha20-Poly1305).
- **Tool-level confirmation for destructive actions.** Gate `shell` and Claude Code "send to session" calls on an additional remote-origin approval step when initiated from relay/Telegram.

Issues and PRs that move any of these forward are welcome.
