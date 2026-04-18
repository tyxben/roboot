# Manual test checklist

Run this before cutting a release. The automated suite (`pytest tests/`)
covers the internals — this covers the parts that only a human with a
browser and a phone can actually exercise. Each item has a one-line
"how", a one-line "pass criterion", and an automated check where one
exists.

Assumes the daemon is running: `python server.py` with config.yaml
configured, and that you're on the same Wi-Fi as any phone you're about
to pair. Relay checks additionally need the Cloudflare Worker deployed
(`cd relay && npx wrangler deploy`).

## 1. Local console smoke

- [ ] **HTTPS root responds.** `curl -sk -o /dev/null -w "%{http_code}\n" https://localhost:8765/` → `200`.
- [ ] **Welcome message appears.** Open https://localhost:8765 in a browser. Pass: `Hey，我是 <name>` bubble shows within 1s of page load.
- [ ] **Text chat round-trip.** Type `hi`, press Enter. Pass: thinking indicator → streaming bubble → final reply, send button re-enables.
- [ ] **Tool invocation.** Ask `list my iTerm2 sessions`. Pass: `list_sessions` tool badge on response, and the sidebar populates with session cards.
- [ ] **Persistence.** After a couple of turns, run `sqlite3 .chat_history.db 'SELECT source, role, substr(content,1,40) FROM messages ORDER BY id DESC LIMIT 4'`. Pass: the last 4 messages match what you just sent/received with `source='local'`.

## 2. Remote (relay) smoke

- [ ] **Pairing URL reachable.** `curl -sk -o /dev/null -w "%{http_code}\n" "$(curl -sk https://localhost:8765/api/relay-info | python -c 'import sys,json; print(json.load(sys.stdin)["pairing_url"])')"` → `200`.
- [ ] **Phone scans QR, page loads.** Open local console → scan the Relay QR on the phone. Pass: page shows `Connected` status within 3s.
- [ ] **E2EE handshake completes.** In daemon log (`/tmp/roboot-server.log`), confirm `[relay][e2ee] handshake complete` line appears after the phone connects (requires `DEBUG_E2EE=1` when starting daemon — otherwise trust that the `Connected` status on the phone implies it).
- [ ] **Chat round-trip from phone.** Send `hi` from phone. Pass: streaming reply arrives. Check `sqlite3 .chat_history.db "SELECT source FROM sessions WHERE source='remote'"` has a new row.
- [ ] **Tab switch button doesn't stick.** Send message → switch to a session tab → switch back → send button is enabled (not greyed). Regression test for commit `164bd89`.
- [ ] **Revoke kicks phone.** On phone, stay on page. On local console, click the red *撤销所有远程访问* button. Pass: phone shows "Access revoked" screen within 2s, no auto-reconnect.
- [ ] **New QR pairs again.** On phone, scan the freshly-generated QR after revoke. Pass: new pairing connects within 3s.

## 3. Soul writes

- [ ] **remember_user appends with date.** Tell the agent something new about yourself (e.g. "remember I use Neovim"). Pass: `grep '($(date +%Y-%m-%d))' soul.md` returns a match under `## About User`.
- [ ] **Snapshot saved.** `ls .soul/history/ | tail -3`. Pass: at least one timestamped `.md` file exists, dated today.

## 4. Reconnection / resilience

- [ ] **Daemon survives WAN blip.** Toggle Wi-Fi off for 10s on the Mac, back on. Pass: daemon log shows `Connection error` then `Connected to relay` without requiring a restart. Existing phone page also reconnects on its own.
- [ ] **No runaway token rotation.** After the blip, pairing URL (`curl -sk https://localhost:8765/api/relay-info`) should be the **same** token/session_id as before. Regression test for commit `fac7192`.

## 5. Telegram (skip if not configured)

- [ ] `python -m adapters.telegram_bot` starts without crashing.
- [ ] `/start` from an allowed user returns a welcome.
- [ ] A chat turn from Telegram creates a `source='telegram'` row in `.chat_history.db`.

---

## How to report

If anything fails, open a GitHub issue with:
- Which checklist item
- Daemon log excerpt (`tail -50 /tmp/roboot-server.log`)
- Browser devtools console if remote-side
- Expected vs. observed behavior

Security-impacting failures: **don't open a public issue.** See SECURITY.md.
