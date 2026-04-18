/**
 * Roboot Relay - Cloudflare Worker entry point
 *
 * Routes incoming requests to the appropriate handler:
 *   /ws/daemon/{id}           -> Durable Object (daemon WebSocket)
 *   /ws/client/{id}           -> Durable Object (client WebSocket)
 *   /pair/{id}                -> Pairing HTML page
 *   /api/health               -> Health check
 *   /api/session/{id}/status  -> Session status
 */

import { renderPairPage } from "./pair-page";

export { RelaySession } from "./relay-session";

interface Env {
  RELAY_SESSIONS: DurableObjectNamespace;
}

/** CORS headers applied to all responses. */
const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
};

/** Simple in-memory rate limiter: IP -> list of timestamps (pruned per check). */
const rateLimitMap = new Map<string, number[]>();
const RATE_LIMIT_WINDOW_MS = 60 * 60 * 1000; // 1 hour
const RATE_LIMIT_MAX_SESSIONS = 10;

function isRateLimited(ip: string): boolean {
  const now = Date.now();
  let timestamps = rateLimitMap.get(ip);
  if (!timestamps) {
    timestamps = [];
    rateLimitMap.set(ip, timestamps);
  }
  // Prune expired entries
  const cutoff = now - RATE_LIMIT_WINDOW_MS;
  const pruned = timestamps.filter((t) => t > cutoff);
  rateLimitMap.set(ip, pruned);
  return pruned.length >= RATE_LIMIT_MAX_SESSIONS;
}

function recordSession(ip: string): void {
  const timestamps = rateLimitMap.get(ip) ?? [];
  timestamps.push(Date.now());
  rateLimitMap.set(ip, timestamps);
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json", ...CORS_HEADERS },
  });
}

function getDurableObjectStub(env: Env, sessionId: string): DurableObjectStub {
  const id = env.RELAY_SESSIONS.idFromName(sessionId);
  // Pin the DO to APAC on first creation. Both target users (Japan + China)
  // are here; without a hint CF may place it anywhere (we've seen latency
  // suggesting transpacific detours). Only affects new DOs — existing ones
  // stay where they were first instantiated. A fresh daemon restart picks
  // a new session_id, which creates a new DO, which gets this hint.
  return env.RELAY_SESSIONS.get(id, { locationHint: "apac" });
}

/** Validate that a string looks like a UUID v4. */
function isValidSessionId(id: string): boolean {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(id);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const path = url.pathname;

    // Handle CORS preflight
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }

    // --- Health check ---
    if (path === "/api/health") {
      return jsonResponse({ status: "ok", version: "1.0" });
    }

    // --- Session status ---
    const statusMatch = path.match(/^\/api\/session\/([^/]+)\/status$/);
    if (statusMatch) {
      const sessionId = statusMatch[1]!;
      if (!isValidSessionId(sessionId)) {
        return jsonResponse({ error: "Invalid session ID" }, 400);
      }
      const stub = getDurableObjectStub(env, sessionId);
      const statusReq = new Request("https://internal/status");
      return stub.fetch(statusReq);
    }

    // --- Pairing page ---
    const pairMatch = path.match(/^\/pair\/([^/]+)$/);
    if (pairMatch) {
      const sessionId = pairMatch[1]!;
      if (!isValidSessionId(sessionId)) {
        return new Response("Invalid session ID", { status: 400 });
      }
      const token = url.searchParams.get("token");
      if (!token) {
        return new Response("Missing pairing token", { status: 401 });
      }
      const host = url.host;
      const protocol = url.protocol === "https:" ? "wss" : "ws";
      return new Response(renderPairPage(sessionId, host, protocol, token), {
        headers: { "Content-Type": "text/html;charset=UTF-8", ...CORS_HEADERS },
      });
    }

    // --- WebSocket upgrade: daemon ---
    const daemonMatch = path.match(/^\/ws\/daemon\/([^/]+)$/);
    if (daemonMatch) {
      const sessionId = daemonMatch[1]!;
      if (!isValidSessionId(sessionId)) {
        return jsonResponse({ error: "Invalid session ID" }, 400);
      }
      if (request.headers.get("Upgrade") !== "websocket") {
        return jsonResponse({ error: "Expected WebSocket upgrade" }, 426);
      }
      const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
      if (isRateLimited(ip)) {
        return jsonResponse({ error: "Rate limit exceeded: max 10 sessions per hour" }, 429);
      }
      recordSession(ip);

      const token = url.searchParams.get("token");
      if (!token || token.length < 32) {
        return jsonResponse({ error: "Missing or invalid token (min 32 chars)" }, 401);
      }

      const stub = getDurableObjectStub(env, sessionId);
      const daemonReq = new Request(
        `https://internal/ws/daemon/${sessionId}?token=${encodeURIComponent(token)}`,
        { headers: request.headers }
      );
      return stub.fetch(daemonReq);
    }

    // --- WebSocket upgrade: client ---
    const clientMatch = path.match(/^\/ws\/client\/([^/]+)$/);
    if (clientMatch) {
      const sessionId = clientMatch[1]!;
      if (!isValidSessionId(sessionId)) {
        return jsonResponse({ error: "Invalid session ID" }, 400);
      }
      if (request.headers.get("Upgrade") !== "websocket") {
        return jsonResponse({ error: "Expected WebSocket upgrade" }, 426);
      }

      const token = url.searchParams.get("token");
      if (!token) {
        return jsonResponse({ error: "Missing pairing token" }, 401);
      }

      const stub = getDurableObjectStub(env, sessionId);
      const clientReq = new Request(
        `https://internal/ws/client/${sessionId}?token=${encodeURIComponent(token)}`,
        { headers: request.headers }
      );
      return stub.fetch(clientReq);
    }

    // --- Fallback ---
    return jsonResponse({ error: "Not found" }, 404);
  },
};
