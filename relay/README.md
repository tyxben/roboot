# Roboot Relay

Cloudflare Worker that relays WebSocket connections between Roboot daemons (Mac) and mobile clients.

```
Mobile Client <--WSS--> Cloudflare Worker (relay.coordbound.com) <--WSS--> Mac Daemon
```

## Architecture

- **Durable Objects** with WebSocket Hibernation API for persistent, cost-efficient sessions
- Each session is identified by a UUID and managed by a single Durable Object instance
- Daemon connects first, then clients can join (up to 5 per session)
- Messages are forwarded transparently (no inspection or modification)

## Prerequisites

- Node.js >= 18
- Cloudflare account with Workers paid plan (Durable Objects require paid)
- `wrangler` CLI authenticated (`npx wrangler login`)

## Setup

```bash
cd relay
npm install
```

## Local Development

```bash
npx wrangler dev
```

This starts the worker locally at `http://localhost:8787`.

## Testing

With the dev server running in one terminal:

```bash
npm test
```

This runs the integration test suite against `http://localhost:8787`.

To test against a deployed instance:

```bash
RELAY_URL=https://relay.coordbound.com npm test
```

## Deployment

```bash
npx wrangler deploy
```

## View Logs

```bash
npx wrangler tail
```

## Custom Domain

To route `relay.coordbound.com` to this worker, uncomment the route config in `wrangler.toml`:

```toml
[routes]
pattern = "relay.coordbound.com/*"
zone_name = "coordbound.com"
```

Then add a DNS record in Cloudflare:
- Type: AAAA
- Name: relay
- Content: 100::
- Proxied: Yes

## Endpoints

| Endpoint | Description |
|---|---|
| `GET /api/health` | Health check: `{"status":"ok","version":"1.0"}` |
| `GET /api/session/{id}/status` | Session status: `{"active":true,"clients":2}` |
| `GET /pair/{id}` | Mobile pairing page (HTML) |
| `WSS /ws/daemon/{id}` | Daemon WebSocket connection |
| `WSS /ws/client/{id}` | Client WebSocket connection |

## WebSocket Protocol

### Daemon

1. Connect to `wss://relay.coordbound.com/ws/daemon/{sessionId}`
2. Send: `{"type":"daemon_hello","version":"1.0"}`
3. Receive client messages (forwarded transparently)
4. Send responses (broadcast to all connected clients)

### Client

1. Connect to `wss://relay.coordbound.com/ws/client/{sessionId}`
2. Send: `{"type":"client_hello"}`
3. Send messages (forwarded to daemon)
4. Receive daemon messages (broadcast)

## Limits

- Max 5 client connections per session
- Session timeout: 1 hour of inactivity
- Rate limit: 10 new sessions per IP per hour
- Only UUIDv4 session IDs accepted

## Cost Considerations

Uses Durable Objects with WebSocket Hibernation API. The DO is evicted from memory between messages, so you only pay for active processing time, not idle connections. Typical cost for low-traffic usage is well under $1/month.
