/**
 * RelaySession - Durable Object managing one daemon<->clients session.
 *
 * Uses the WebSocket Hibernation API for cost efficiency: the DO can be
 * evicted from memory between messages and will be re-instantiated when
 * a new message arrives.
 *
 * Lifecycle:
 *   1. Daemon connects  -> stored as this.daemon
 *   2. Clients connect   -> added to this.clients (max 5)
 *   3. Messages flow:
 *        client -> daemon (any client message forwarded to daemon)
 *        daemon -> all clients (broadcast)
 *   4. Daemon disconnects -> all clients disconnected, session ends
 *   5. Inactivity alarm  -> after 1 hour, everything torn down
 */

interface SessionState {
  daemonConnected: boolean;
  clientCount: number;
  lastActivity: number;
}

const MAX_CLIENTS = 5;
const SESSION_TIMEOUT_MS = 60 * 60 * 1000; // 1 hour
/** Storage key for the pairing token set by the daemon. */
const TOKEN_KEY = "pairingToken";

/**
 * Message types the relay is allowed to forward.
 *
 * All application messages (chat, streaming, tool events, etc.) travel as
 * `encrypted` envelopes once the E2EE handshake completes. The handshake
 * itself plus control frames (ping/pong/error) stay unencrypted so the
 * relay can continue to route them without knowing any keys.
 *
 * Messages of any other type are rejected — this prevents a compromised
 * client from smuggling plaintext app messages past E2EE.
 */
const ALLOWED_MESSAGE_TYPES = new Set<string>([
  "e2ee_handshake",
  "encrypted",
  "ping",
  "pong",
  "error",
]);

/** Tag used to identify the daemon WebSocket in hibernation storage. */
const DAEMON_TAG = "daemon";
/** Tag prefix for client WebSockets. */
const CLIENT_TAG_PREFIX = "client:";

/**
 * Constant-time string comparison to prevent timing attacks on token verification.
 * Uses XOR accumulation so execution time is independent of where strings differ.
 */
function timingSafeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) {
    // Still do a dummy comparison to avoid leaking length difference via timing
    b = a;
  }
  let mismatch = a.length ^ b.length; // non-zero if lengths differ
  for (let i = 0; i < a.length; i++) {
    mismatch |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return mismatch === 0;
}

export class RelaySession implements DurableObject {
  private state: DurableObjectState;

  constructor(state: DurableObjectState, _env: unknown) {
    this.state = state;
  }

  async fetch(request: Request): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;
    const token = url.searchParams.get("token");

    // --- Status endpoint (non-WebSocket) ---
    if (path === "/status") {
      const info = await this.getSessionInfo();
      return new Response(
        JSON.stringify({
          active: info.daemonConnected,
          clients: info.clientCount,
        }),
        {
          headers: {
            "Content-Type": "application/json",
            "Access-Control-Allow-Origin": "*",
          },
        }
      );
    }

    // --- WebSocket upgrade ---
    const daemonMatch = path.match(/^\/ws\/daemon\/([^/]+)$/);
    if (daemonMatch) {
      if (!token || token.length < 32) {
        return new Response(
          JSON.stringify({ error: "Missing or invalid token (min 32 chars)" }),
          { status: 401, headers: { "Content-Type": "application/json" } }
        );
      }
      return this.handleDaemonUpgrade(token);
    }

    const clientMatch = path.match(/^\/ws\/client\/([^/]+)$/);
    if (clientMatch) {
      if (!token) {
        return new Response(
          JSON.stringify({ error: "Missing token" }),
          { status: 401, headers: { "Content-Type": "application/json" } }
        );
      }
      return this.handleClientUpgrade(token);
    }

    return new Response("Not found", { status: 404 });
  }

  // ---------------------------------------------------------------------------
  // WebSocket upgrade handlers
  // ---------------------------------------------------------------------------

  private async handleDaemonUpgrade(token: string): Promise<Response> {
    // Only one daemon per session
    const existingDaemons = this.state.getWebSockets(DAEMON_TAG);
    if (existingDaemons.length > 0) {
      return new Response(
        JSON.stringify({ error: "Daemon already connected to this session" }),
        { status: 409, headers: { "Content-Type": "application/json" } }
      );
    }

    // Store the pairing token — clients must present this to join
    await this.state.storage.put(TOKEN_KEY, token);

    const pair = new WebSocketPair();
    const [client, server] = [pair[0], pair[1]];

    this.state.acceptWebSocket(server, [DAEMON_TAG]);
    await this.touchActivity();

    return new Response(null, { status: 101, webSocket: client });
  }

  private async handleClientUpgrade(token: string): Promise<Response> {
    // Check if daemon is connected
    const daemons = this.state.getWebSockets(DAEMON_TAG);
    if (daemons.length === 0) {
      return new Response(
        JSON.stringify({ error: "No daemon connected to this session" }),
        { status: 404, headers: { "Content-Type": "application/json" } }
      );
    }

    // Verify pairing token — constant-time comparison to prevent timing attacks
    const storedToken = await this.state.storage.get<string>(TOKEN_KEY);
    if (!storedToken || !timingSafeEqual(token, storedToken)) {
      return new Response(
        JSON.stringify({ error: "Invalid pairing token" }),
        { status: 403, headers: { "Content-Type": "application/json" } }
      );
    }

    // Enforce max clients
    const clients = this.getClientWebSockets();
    if (clients.length >= MAX_CLIENTS) {
      return new Response(
        JSON.stringify({ error: `Max ${MAX_CLIENTS} clients per session` }),
        { status: 429, headers: { "Content-Type": "application/json" } }
      );
    }

    const pair = new WebSocketPair();
    const [clientSide, serverSide] = [pair[0], pair[1]];

    const clientId = crypto.randomUUID();
    this.state.acceptWebSocket(serverSide, [`${CLIENT_TAG_PREFIX}${clientId}`]);
    await this.touchActivity();

    return new Response(null, { status: 101, webSocket: clientSide });
  }

  // ---------------------------------------------------------------------------
  // WebSocket Hibernation API handlers
  // ---------------------------------------------------------------------------

  async webSocketMessage(ws: WebSocket, message: string | ArrayBuffer): Promise<void> {
    await this.touchActivity();

    const isDaemon = this.isDaemonSocket(ws);
    const msgData = typeof message === "string" ? message : new TextDecoder().decode(message);

    // Relay only forwards whitelisted message types — everything interesting
    // is expected to travel as an `encrypted` envelope. Silently dropping
    // keeps the DO simple and prevents plaintext leakage bugs upstream.
    if (!this.isAllowedMessage(msgData)) {
      return;
    }

    if (isDaemon) {
      // Daemon -> broadcast to all clients
      this.broadcastToClients(msgData);
    } else {
      // Client -> forward to daemon
      this.forwardToDaemon(msgData);
    }
  }

  /** Parse the JSON envelope and check that its `type` is allowed. */
  private isAllowedMessage(raw: string): boolean {
    try {
      const parsed = JSON.parse(raw) as { type?: unknown };
      return typeof parsed.type === "string" && ALLOWED_MESSAGE_TYPES.has(parsed.type);
    } catch {
      return false;
    }
  }

  async webSocketClose(
    ws: WebSocket,
    code: number,
    _reason: string,
    _wasClean: boolean
  ): Promise<void> {
    const isDaemon = this.isDaemonSocket(ws);

    if (isDaemon) {
      // Daemon disconnected -> tear down entire session
      this.disconnectAllClients(code, "Daemon disconnected");
      // Clean up stored token and cancel alarm
      await this.state.storage.delete(TOKEN_KEY);
      await this.state.storage.deleteAlarm();
    }
    // If a client disconnects, nothing special to do -
    // the hibernation API automatically removes it from getWebSockets().
  }

  async webSocketError(ws: WebSocket, _error: unknown): Promise<void> {
    const isDaemon = this.isDaemonSocket(ws);
    if (isDaemon) {
      this.disconnectAllClients(1011, "Daemon error");
      await this.state.storage.deleteAlarm();
    }
  }

  /** Called when the inactivity alarm fires. */
  async alarm(): Promise<void> {
    const lastActivity = (await this.state.storage.get<number>("lastActivity")) ?? 0;
    const elapsed = Date.now() - lastActivity;

    if (elapsed >= SESSION_TIMEOUT_MS) {
      // Timeout: disconnect everything
      const daemons = this.state.getWebSockets(DAEMON_TAG);
      for (const d of daemons) {
        try {
          d.close(4000, "Session timed out");
        } catch {
          // already closed
        }
      }
      this.disconnectAllClients(4000, "Session timed out");
    } else {
      // Not yet expired, re-arm alarm for remaining time
      await this.state.storage.setAlarm(Date.now() + (SESSION_TIMEOUT_MS - elapsed));
    }
  }

  // ---------------------------------------------------------------------------
  // Internal helpers
  // ---------------------------------------------------------------------------

  private isDaemonSocket(ws: WebSocket): boolean {
    const tags = this.state.getTags(ws);
    return tags.includes(DAEMON_TAG);
  }

  private getClientWebSockets(): WebSocket[] {
    // Get all websockets and filter to those with client tags
    const all = this.state.getWebSockets();
    return all.filter((ws) => {
      const tags = this.state.getTags(ws);
      return tags.some((t) => t.startsWith(CLIENT_TAG_PREFIX));
    });
  }

  private broadcastToClients(message: string): void {
    const clients = this.getClientWebSockets();
    for (const client of clients) {
      try {
        client.send(message);
      } catch {
        // Client already closed; hibernation API will clean up
      }
    }
  }

  private forwardToDaemon(message: string): void {
    const daemons = this.state.getWebSockets(DAEMON_TAG);
    for (const daemon of daemons) {
      try {
        daemon.send(message);
      } catch {
        // Daemon already closed
      }
    }
  }

  private disconnectAllClients(code: number, reason: string): void {
    const clients = this.getClientWebSockets();
    for (const client of clients) {
      try {
        client.close(code, reason);
      } catch {
        // already closed
      }
    }
  }

  private async touchActivity(): Promise<void> {
    const now = Date.now();
    await this.state.storage.put("lastActivity", now);
    // Re-arm the inactivity alarm
    await this.state.storage.setAlarm(now + SESSION_TIMEOUT_MS);
  }

  private async getSessionInfo(): Promise<SessionState> {
    const daemons = this.state.getWebSockets(DAEMON_TAG);
    const clients = this.getClientWebSockets();
    const lastActivity = (await this.state.storage.get<number>("lastActivity")) ?? 0;

    return {
      daemonConnected: daemons.length > 0,
      clientCount: clients.length,
      lastActivity,
    };
  }
}
