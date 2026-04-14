/**
 * Integration test for the Roboot relay.
 *
 * Requires the relay to be running locally:
 *   cd relay && npx wrangler dev
 *
 * Then in another terminal:
 *   cd relay && npm test
 *
 * Tests:
 *   1. Health endpoint responds correctly
 *   2. Session status for non-existent session returns inactive
 *   3. Daemon WebSocket connects with token
 *   4. Client WebSocket rejected without valid token
 *   5. Client -> daemon message forwarding
 *   6. Daemon -> client message forwarding (broadcast)
 *   7. Daemon disconnect tears down clients
 *   8. Token mismatch rejected
 */

import WebSocket from "ws";
import { randomBytes, randomUUID } from "node:crypto";

const BASE_URL = process.env.RELAY_URL ?? "http://localhost:8787";
const WS_BASE = BASE_URL.replace(/^http/, "ws");

function uuid(): string {
  return randomUUID();
}

/** Generate a 64-char hex token (256-bit), matching relay_client.py's secrets.token_hex(32). */
function generateToken(): string {
  return randomBytes(32).toString("hex");
}

let passed = 0;
let failed = 0;

function assert(condition: boolean, label: string): void {
  if (condition) {
    console.log(`  PASS: ${label}`);
    passed++;
  } else {
    console.error(`  FAIL: ${label}`);
    failed++;
  }
}

function connectWs(path: string): Promise<WebSocket> {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(`${WS_BASE}${path}`);
    ws.on("open", () => resolve(ws));
    ws.on("error", reject);
    setTimeout(() => reject(new Error(`WS connect timeout: ${path}`)), 5000);
  });
}

function waitForMessage(ws: WebSocket, timeoutMs = 3000): Promise<string> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Message timeout")), timeoutMs);
    ws.once("message", (data) => {
      clearTimeout(timer);
      resolve(data.toString());
    });
  });
}

function waitForClose(ws: WebSocket, timeoutMs = 3000): Promise<{ code: number; reason: string }> {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Close timeout")), timeoutMs);
    ws.once("close", (code: number, reason: Buffer) => {
      clearTimeout(timer);
      resolve({ code, reason: reason.toString() });
    });
  });
}

async function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

async function testHealth(): Promise<void> {
  console.log("\n--- Test: Health endpoint ---");
  const resp = await fetch(`${BASE_URL}/api/health`);
  const data = (await resp.json()) as { status: string; version: string };
  assert(resp.status === 200, "Status 200");
  assert(data.status === "ok", "status is ok");
  assert(data.version === "1.0", "version is 1.0");
}

async function testSessionStatusInactive(): Promise<void> {
  console.log("\n--- Test: Inactive session status ---");
  const id = uuid();
  const resp = await fetch(`${BASE_URL}/api/session/${id}/status`);
  const data = (await resp.json()) as { active: boolean; clients: number };
  assert(resp.status === 200, "Status 200");
  assert(data.active === false, "Session is not active");
  assert(data.clients === 0, "Zero clients");
}

async function testDaemonAndClientFlow(): Promise<void> {
  console.log("\n--- Test: Daemon + Client message flow ---");
  const sessionId = uuid();
  const token = generateToken();

  // 1. Connect daemon with token
  console.log("  Connecting daemon...");
  const daemon = await connectWs(`/ws/daemon/${sessionId}?token=${token}`);
  daemon.send(JSON.stringify({ type: "daemon_hello", version: "1.0" }));
  await sleep(200);

  // 2. Check session is active
  const statusResp = await fetch(`${BASE_URL}/api/session/${sessionId}/status`);
  const status = (await statusResp.json()) as { active: boolean; clients: number };
  assert(status.active === true, "Session active after daemon connects");

  // 3. Connect client with same token
  console.log("  Connecting client...");
  const client = await connectWs(`/ws/client/${sessionId}?token=${token}`);
  client.send(JSON.stringify({ type: "client_hello" }));
  await sleep(200);

  // 4. Client -> Daemon
  console.log("  Testing client -> daemon...");
  const daemonMsgPromise = waitForMessage(daemon);
  const clientPayload = JSON.stringify({ type: "chat", text: "Hello from client" });
  client.send(clientPayload);
  const daemonReceived = await daemonMsgPromise;
  assert(daemonReceived === clientPayload, "Daemon received client message");

  // 5. Daemon -> Client (broadcast)
  console.log("  Testing daemon -> client...");
  const clientMsgPromise = waitForMessage(client);
  const daemonPayload = JSON.stringify({ type: "response", text: "Hello from daemon" });
  daemon.send(daemonPayload);
  const clientReceived = await clientMsgPromise;
  assert(clientReceived === daemonPayload, "Client received daemon broadcast");

  // 6. Session status shows 1 client
  const statusResp2 = await fetch(`${BASE_URL}/api/session/${sessionId}/status`);
  const status2 = (await statusResp2.json()) as { active: boolean; clients: number };
  assert(status2.clients === 1, "Status shows 1 client connected");

  // Cleanup
  daemon.close();
  client.close();
  await sleep(300);
}

async function testMultipleClients(): Promise<void> {
  console.log("\n--- Test: Multiple clients ---");
  const sessionId = uuid();
  const token = generateToken();

  const daemon = await connectWs(`/ws/daemon/${sessionId}?token=${token}`);
  daemon.send(JSON.stringify({ type: "daemon_hello", version: "1.0" }));
  await sleep(200);

  // Connect 3 clients with valid token
  const clients: WebSocket[] = [];
  for (let i = 0; i < 3; i++) {
    const c = await connectWs(`/ws/client/${sessionId}?token=${token}`);
    c.send(JSON.stringify({ type: "client_hello" }));
    clients.push(c);
  }
  await sleep(200);

  // Daemon broadcasts -> all clients receive
  const promises = clients.map((c) => waitForMessage(c));
  const broadcastMsg = JSON.stringify({ type: "broadcast", data: "to all" });
  daemon.send(broadcastMsg);

  const results = await Promise.all(promises);
  const allReceived = results.every((r) => r === broadcastMsg);
  assert(allReceived, "All 3 clients received daemon broadcast");

  // Cleanup
  daemon.close();
  for (const c of clients) c.close();
  await sleep(300);
}

async function testDaemonDisconnectCleansUp(): Promise<void> {
  console.log("\n--- Test: Daemon disconnect cleans up clients ---");
  const sessionId = uuid();
  const token = generateToken();

  const daemon = await connectWs(`/ws/daemon/${sessionId}?token=${token}`);
  daemon.send(JSON.stringify({ type: "daemon_hello", version: "1.0" }));
  await sleep(200);

  const client = await connectWs(`/ws/client/${sessionId}?token=${token}`);
  client.send(JSON.stringify({ type: "client_hello" }));
  await sleep(200);

  const closePromise = waitForClose(client, 5000);
  daemon.close();

  try {
    const closeEvent = await closePromise;
    assert(true, `Client disconnected with code ${closeEvent.code}: ${closeEvent.reason}`);
  } catch {
    assert(false, "Client should have been disconnected when daemon left");
  }
}

async function testClientBeforeDaemon(): Promise<void> {
  console.log("\n--- Test: Client before daemon gets rejected ---");
  const sessionId = uuid();
  const token = generateToken();

  try {
    await connectWs(`/ws/client/${sessionId}?token=${token}`);
    assert(false, "Should have been rejected");
  } catch {
    assert(true, "Client rejected when no daemon is connected");
  }
}

async function testInvalidSessionId(): Promise<void> {
  console.log("\n--- Test: Invalid session ID ---");
  const resp = await fetch(`${BASE_URL}/api/session/not-a-uuid/status`);
  assert(resp.status === 400, "Returns 400 for invalid session ID");
}

async function testWrongTokenRejected(): Promise<void> {
  console.log("\n--- Test: Wrong token rejected ---");
  const sessionId = uuid();
  const daemonToken = generateToken();
  const wrongToken = generateToken();

  // Daemon connects with its token
  const daemon = await connectWs(`/ws/daemon/${sessionId}?token=${daemonToken}`);
  daemon.send(JSON.stringify({ type: "daemon_hello", version: "1.0" }));
  await sleep(200);

  // Client tries with a different token -> should be rejected (403)
  try {
    await connectWs(`/ws/client/${sessionId}?token=${wrongToken}`);
    assert(false, "Client with wrong token should have been rejected");
  } catch {
    assert(true, "Client with wrong token rejected");
  }

  daemon.close();
  await sleep(200);
}

async function testMissingTokenRejected(): Promise<void> {
  console.log("\n--- Test: Missing token rejected ---");
  const sessionId = uuid();

  // Daemon without token -> should be rejected (401)
  try {
    await connectWs(`/ws/daemon/${sessionId}`);
    assert(false, "Daemon without token should have been rejected");
  } catch {
    assert(true, "Daemon without token rejected");
  }
}

async function testShortTokenRejected(): Promise<void> {
  console.log("\n--- Test: Short token rejected ---");
  const sessionId = uuid();

  // Daemon with too-short token -> should be rejected
  try {
    await connectWs(`/ws/daemon/${sessionId}?token=tooshort`);
    assert(false, "Daemon with short token should have been rejected");
  } catch {
    assert(true, "Daemon with short token rejected");
  }
}

// ---------------------------------------------------------------------------
// Runner
// ---------------------------------------------------------------------------

async function main(): Promise<void> {
  console.log(`\nRoboot Relay Test Suite (with token auth)`);
  console.log(`Target: ${BASE_URL}\n`);

  try {
    await testHealth();
    await testSessionStatusInactive();
    await testInvalidSessionId();
    await testMissingTokenRejected();
    await testShortTokenRejected();
    await testClientBeforeDaemon();
    await testDaemonAndClientFlow();
    await testWrongTokenRejected();
    await testMultipleClients();
    await testDaemonDisconnectCleansUp();
  } catch (err) {
    console.error("\nFATAL ERROR:", err);
    failed++;
  }

  console.log(`\n${"=".repeat(40)}`);
  console.log(`Results: ${passed} passed, ${failed} failed`);
  console.log(`${"=".repeat(40)}\n`);

  process.exit(failed > 0 ? 1 : 0);
}

main();
