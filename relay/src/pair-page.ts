/**
 * Remote console — serves the full Roboot console UI adapted for relay mode.
 *
 * Changes from local console.html:
 * - WebSocket connects to relay instead of local server
 * - Sends {type:"client_hello"} on connect, {type:"chat",content:...} for messages
 * - Sidebar sessions disabled (no local iTerm2 access)
 * - TTS/JARVIS disabled (no local /api/tts)
 * - Network panel disabled (no local /api/network-info)
 * - PWA service worker removed
 */

export function renderPairPage(sessionId: string, host: string, wsProtocol: string, token: string): string {
  const wsUrl = `${wsProtocol}://${host}/ws/client/${sessionId}?token=${encodeURIComponent(token)}`;

  // Full console.html with relay adaptations injected
  return CONSOLE_HTML.replace("__RELAY_WS_URL__", wsUrl);
}

const CONSOLE_HTML = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="theme-color" content="#e94560">
<title>Roboot Console</title>
<style>
* { margin: 0; padding: 0; box-sizing: border-box; }

:root {
  --bg: #0d1117;
  --surface: #161b22;
  --surface2: #1c2333;
  --border: rgba(255,255,255,0.06);
  --accent: #e94560;
  --green: #3fb950;
  --yellow: #d29922;
  --red: #f85149;
  --text: #e6edf3;
  --text-dim: #7d8590;
  --radius: 10px;
  --sidebar-w: 280px;
}

body {
  font-family: -apple-system, "PingFang SC", "SF Pro", system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  height: 100vh;
  height: calc(var(--vh, 1vh) * 100);
  display: flex;
  overflow: hidden;
}

/* === SIDEBAR === */
.sidebar {
  width: var(--sidebar-w);
  background: var(--surface);
  border-right: 1px solid var(--border);
  display: flex; flex-direction: column; flex-shrink: 0;
}

.sidebar-header { padding: 16px; border-bottom: 1px solid var(--border); }

.sidebar-header h1 {
  font-size: 15px; font-weight: 600;
  display: flex; align-items: center; gap: 8px;
}

.sidebar-header h1 .logo {
  width: 24px; height: 24px; border-radius: 6px;
  background: linear-gradient(135deg, var(--accent), #533483);
  display: flex; align-items: center; justify-content: center;
  font-size: 13px; font-weight: 700;
}

.sidebar-header .count { font-size: 12px; color: var(--text-dim); margin-top: 4px; }

.session-list {
  flex: 1; overflow-y: auto; padding: 8px;
  -webkit-overflow-scrolling: touch;
}

.sidebar-footer { padding: 8px; border-top: 1px solid var(--border); }

.chat-tab {
  padding: 10px 12px; border-radius: 8px;
  cursor: pointer; font-size: 13px; font-weight: 500;
  display: flex; align-items: center; gap: 8px;
}
.chat-tab:hover { background: rgba(255,255,255,0.04); }
.chat-tab.active { background: rgba(233,69,96,0.12); color: var(--accent); }

.chat-tab .icon {
  width: 20px; height: 20px; border-radius: 5px;
  background: var(--accent);
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; color: white;
}

/* === MAIN === */
.main { flex: 1; display: flex; flex-direction: column; min-width: 0; }

.panel { display: none; flex: 1; flex-direction: column; overflow: hidden; }
.panel.visible { display: flex; }

.panel-header {
  padding: 12px 20px; border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 12px;
  background: var(--surface); flex-shrink: 0;
}
.panel-header .title { flex: 1; }
.panel-header .title h2 { font-size: 14px; font-weight: 600; }
.panel-header .title .meta { font-size: 11px; color: var(--text-dim); margin-top: 2px; }

.panel-header button {
  padding: 5px 12px; border: none; border-radius: 6px;
  font-size: 12px; cursor: pointer; font-family: inherit;
  background: var(--surface2); color: var(--text);
}
.panel-header button:hover { background: rgba(255,255,255,0.1); }
.panel-header button.on { background: var(--accent); color: white; }

/* Chat */
.chat-messages {
  flex: 1; overflow-y: auto; padding: 16px 20px;
  display: flex; flex-direction: column; gap: 10px;
  -webkit-overflow-scrolling: touch;
}
.chat-messages::-webkit-scrollbar { width: 4px; }
.chat-messages::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.08); border-radius: 2px; }

.chat-msg {
  max-width: 80%; padding: 9px 13px;
  border-radius: var(--radius); font-size: 13.5px;
  line-height: 1.55; word-break: break-word;
  animation: fadeUp 0.15s ease;
}
@keyframes fadeUp { from{opacity:0;transform:translateY(6px)} to{opacity:1;transform:translateY(0)} }

.chat-msg.user { align-self: flex-end; background: #1f3a5f; border-bottom-right-radius: 4px; }
.chat-msg.bot { align-self: flex-start; background: var(--surface2); border-bottom-left-radius: 4px; }

.chat-msg.thinking {
  align-self: flex-start; color: var(--text-dim);
  background: transparent; font-size: 12px; padding: 6px 0;
}
.chat-msg.thinking::after { content:""; animation: dots 1.2s steps(4) infinite; }
@keyframes dots { 0%{content:""} 25%{content:"."} 50%{content:".."} 75%{content:"..."} }

.chat-msg pre {
  background: rgba(0,0,0,0.3); padding: 6px 8px; border-radius: 6px;
  margin-top: 5px; overflow-x: auto; font-size: 12px;
  font-family: "SF Mono", "Menlo", monospace;
}
.chat-msg code {
  background: rgba(0,0,0,0.2); padding: 1px 4px; border-radius: 3px;
  font-size: 12px; font-family: "SF Mono", "Menlo", monospace;
}

.tools-badge {
  font-size: 10px; color: var(--accent);
  background: rgba(233,69,96,0.1);
  padding: 2px 7px; border-radius: 8px;
  margin-top: 5px; display: inline-block;
}

.toast {
  position: fixed; top: 16px; right: 16px;
  background: var(--green); color: white;
  padding: 8px 16px; border-radius: 8px;
  font-size: 12px; opacity: 0; transition: opacity 0.25s; z-index: 50;
}
.toast.show { opacity: 1; }

/* Input bar */
.input-bar {
  padding: 10px 14px; border-top: 1px solid var(--border);
  display: flex; gap: 8px; align-items: flex-end;
  background: var(--surface); flex-shrink: 0;
}

.input-bar textarea {
  flex: 1; background: var(--bg);
  border: 1px solid var(--border); border-radius: 8px;
  padding: 8px 12px; color: var(--text); font-size: 13px;
  resize: none; outline: none; max-height: 100px; line-height: 1.4;
}
.input-bar textarea:focus { border-color: var(--accent); }
.input-bar textarea::placeholder { color: var(--text-dim); }

.input-bar button {
  width: 34px; height: 34px; border: none; border-radius: 8px;
  background: var(--accent); color: white; font-size: 15px;
  cursor: pointer; flex-shrink: 0;
}
.input-bar button:disabled { opacity: 0.3; cursor: default; }

/* Session list items */
.session-item {
  padding: 10px 12px; border-radius: 8px;
  cursor: pointer; margin-bottom: 2px;
  transition: background 0.12s;
  display: flex; align-items: center; gap: 8px;
}
.session-item:hover { background: rgba(255,255,255,0.04); }
.session-item.active { background: rgba(233,69,96,0.12); }
.session-item .dot {
  width: 6px; height: 6px; border-radius: 50%;
  background: var(--green); flex-shrink: 0;
}
.session-item .dot.waiting { background: var(--yellow); animation: pulse 1.5s ease infinite; }
@keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.35; } }
.session-item .label { font-size: 13px; font-weight: 500; flex: 1; }

/* Confirm bar — shown when Claude Code is waiting for approval */
.confirm-bar {
  display: none;
  padding: 10px 14px;
  border-top: 1px solid var(--border);
  background: rgba(210,153,34,0.08);
  align-items: center; gap: 10px; flex-shrink: 0;
}
.confirm-bar.visible { display: flex; }
.confirm-bar .prompt {
  flex: 1; font-size: 12.5px; color: var(--yellow);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  font-family: "SF Mono","Menlo",monospace;
}
.confirm-bar button {
  padding: 6px 14px; border: none; border-radius: 6px;
  font-size: 12.5px; cursor: pointer; font-family: inherit; font-weight: 600;
}
.btn-allow { background: var(--green); color: #000; }
.btn-deny { background: var(--red); color: white; }
.btn-allow:hover { opacity: 0.9; }
.btn-deny:hover { opacity: 0.9; }

/* Terminal */
.terminal {
  flex: 1; overflow-y: auto; padding: 14px 18px;
  font-family: "SF Mono", "Menlo", monospace;
  font-size: 12.5px; line-height: 1.55;
  white-space: pre-wrap; word-break: break-all;
  color: #adbac7; background: var(--bg);
  -webkit-overflow-scrolling: touch;
}

/* Connection status bar */
.conn-status {
  padding: 4px 20px; font-size: 11px; text-align: center;
  background: rgba(248,81,73,0.15); color: var(--red);
  display: none;
}
.conn-status.show { display: block; }
.conn-status.online { background: rgba(63,185,80,0.1); color: var(--green); }

/* === MOBILE RESPONSIVE === */
@media (max-width: 768px) {
  :root { --sidebar-w: 0px; }
  body { flex-direction: column; }

  .sidebar {
    position: fixed; top: 0; left: -280px; width: 280px;
    height: 100vh; height: calc(var(--vh, 1vh) * 100);
    z-index: 100; transition: left 0.3s ease;
    box-shadow: 2px 0 12px rgba(0,0,0,0.5);
  }
  .sidebar.open { left: 0; }

  .main {
    width: 100%;
    height: 100vh; height: calc(var(--vh, 1vh) * 100);
  }

  .mobile-header {
    display: flex !important;
    padding: 12px 16px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
    align-items: center; gap: 12px;
  }

  .menu-toggle {
    width: 36px; height: 36px; border: none;
    background: var(--surface2); color: var(--text);
    border-radius: 8px; font-size: 18px; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }

  .mobile-header .title { flex: 1; font-size: 15px; font-weight: 600; }

  .panel-header { display: none; }

  .chat-messages { padding: 12px; padding-bottom: 80px !important; }

  .input-bar {
    padding: 8px 12px;
    padding-bottom: calc(8px + env(safe-area-inset-bottom) + 60px);
    position: relative; z-index: 10;
  }

  .input-bar textarea { font-size: 16px !important; }

  .chat-msg { max-width: 90%; font-size: 14px; }

  .sidebar-backdrop {
    display: none; position: fixed; top: 0; left: 0;
    width: 100%; height: 100%;
    background: rgba(0,0,0,0.5); z-index: 99;
  }
  .sidebar-backdrop.open { display: block; }
}

@media (min-width: 769px) {
  .mobile-header { display: none; }
  .sidebar-backdrop { display: none !important; }
}
</style>
</head>
<body>

<div class="sidebar-backdrop" id="sidebar-backdrop" onclick="closeSidebar()"></div>

<div class="sidebar" id="sidebar">
  <div class="sidebar-header">
    <h1><span class="logo">R</span> Roboot</h1>
    <div class="count" id="session-count"></div>
  </div>
  <div class="session-list" id="session-list"></div>
  <div class="sidebar-footer">
    <div class="chat-tab active" id="chat-tab" onclick="showChat()">
      <span class="icon">AI</span> Roboot Chat
    </div>
  </div>
</div>

<div class="main">
  <div class="mobile-header">
    <button class="menu-toggle" onclick="toggleSidebar()">&#9776;</button>
    <div class="title" id="mobile-title">Roboot</div>
  </div>

  <div class="conn-status" id="conn-status">Connecting...</div>

  <div class="panel" id="panel-session">
    <div class="panel-header">
      <div class="title">
        <h2 id="sv-title"></h2>
        <div class="meta" id="sv-meta"></div>
      </div>
      <button id="auto-btn" onclick="toggleAuto()">Auto Refresh</button>
    </div>
    <div class="terminal" id="terminal"></div>
    <div class="confirm-bar" id="confirm-bar">
      <span class="prompt" id="confirm-prompt">Waiting for confirmation...</span>
      <button class="btn-allow" onclick="quickSend('y')">Allow</button>
      <button class="btn-deny" onclick="quickSend('n')">Deny</button>
    </div>
    <div class="input-bar">
      <textarea id="txt-session" rows="1" placeholder="Send to this session..." style="font-family:'SF Mono','Menlo',monospace;"></textarea>
      <button onclick="sendToSession()">&#8593;</button>
    </div>
  </div>

  <div class="panel visible" id="panel-chat">
    <div class="panel-header">
      <div class="title">
        <h2 id="chat-title">Roboot</h2>
        <div class="meta" id="chat-meta">Remote via relay</div>
      </div>
      <button id="jarvis-btn" onclick="toggleJarvis()" style="padding:5px 14px;border:none;border-radius:6px;font-size:12px;cursor:pointer;font-family:inherit;background:var(--surface2);color:var(--text);">JARVIS</button>
    </div>
    <div id="jarvis-orb" style="display:none;flex-shrink:0;padding:20px 0;text-align:center;">
      <div id="orb" style="width:80px;height:80px;border-radius:50%;margin:0 auto;background:radial-gradient(circle,rgba(233,69,96,0.6),rgba(83,52,131,0.3));box-shadow:0 0 40px rgba(233,69,96,0.3);transition:all 0.3s;"></div>
      <div id="jarvis-status" style="margin-top:10px;font-size:13px;color:var(--text-dim);"></div>
    </div>
    <div class="chat-messages" id="chat-messages"></div>
    <div class="input-bar">
      <button id="mic-btn" onclick="toggleMic()" title="Voice input" style="width:34px;height:34px;border:none;border-radius:8px;background:var(--surface2);color:var(--text-dim);font-size:16px;cursor:pointer;flex-shrink:0;">&#127908;</button>
      <textarea id="txt-chat" rows="1" placeholder="Say something..."></textarea>
      <button id="chat-send-btn" onclick="chatSend()">&#8593;</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
var RELAY_WS_URL = "__RELAY_WS_URL__";
var ws = null;
var thinkingEl = null;
var streamBubble = null;
var streamText = '';
var renderTimer = null;
var connected = false;
var reconnectDelay = 1000;
var sessions = [];
var currentId = null;
// autoOn = user intent (stays true even when we pause the timer because the
// tab is hidden); autoTimer = actual setInterval handle, nulled when paused.
var autoOn = false;
var autoTimer = null;
// True once the user has opened the sidebar (or we're on desktop where it's
// always visible). Persists across reconnects so the daemon re-subscribes.
var sessionsWanted = false;
// Per-session incremental cursor. undefined => need full initial fetch.
var lastLineBySession = {};
// Pattern matches Claude Code waiting-for-approval prompts. Scanned over
// the last ~10 lines of terminal output each refresh; when a pattern hits,
// the confirm bar appears with Allow/Deny buttons that send y/n.
var CONFIRM_PATTERNS = [
  /Do you want to proceed/i, /\\[Y\\/n\\]/, /\\[y\\/N\\]/, /\\(y\\/n\\)/i,
  /Allow|Deny/, /allow this/i, /approve this/i,
  /Press Enter to continue/i, /Do you want to/i, /Shall I/i,
  /要继续吗/, /是否允许/, /确认/,
];
var ANSI_RE = /\\x1b\\[[0-9;?]*[ -\\/]*[@-~]|\\x1b\\].*?(?:\\x07|\\x1b\\\\)/g;
function stripAnsi(s) { return s.replace(ANSI_RE, ''); }
// Heartbeat: send ping every 30s; if no pong within 60s close + reconnect.
var HEARTBEAT_INTERVAL_MS = 30000;
var HEARTBEAT_TIMEOUT_MS = 60000;
var _hbPingTimer = null;
var _hbWatchdog = null;
var _lastPongAt = 0;
// Revoked flag -- when set, suppress auto-reconnect and show final screen.
var _revoked = false;

// === E2EE state ===
// Each WebSocket connection gets a fresh ECDH keypair + derived AES-GCM key.
// All app messages are wrapped in {type:"encrypted",iv,ct}; the handshake
// itself travels in the clear so the Cloudflare relay can still route it.
var CLIENT_ID = (crypto.randomUUID ? crypto.randomUUID() :
  ('xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx').replace(/[xy]/g, function(c) {
    var r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 0x3 | 0x8)).toString(16);
  }));
var e2eeKey = null;        // CryptoKey for AES-GCM (after handshake)
var e2eeReady = false;     // true once both pubkeys have been exchanged
var e2eeKeypair = null;    // our ephemeral ECDH keypair for this WS
var preHandshakeQueue = []; // outbound app messages buffered until handshake done
var DEBUG_E2EE = false;    // flip to true in devtools to log (meta only, no plaintext)

function _b64FromBytes(bytes) {
  var s = '';
  for (var i = 0; i < bytes.length; i++) s += String.fromCharCode(bytes[i]);
  return btoa(s);
}
function _bytesFromB64(b64) {
  var s = atob(b64);
  var out = new Uint8Array(s.length);
  for (var i = 0; i < s.length; i++) out[i] = s.charCodeAt(i);
  return out;
}

function resetE2EE() {
  e2eeKey = null;
  e2eeReady = false;
  e2eeKeypair = null;
  preHandshakeQueue = [];
}

async function startHandshake() {
  // Generate an ephemeral P-256 keypair, ship the raw pubkey to the daemon.
  // deriveBits is needed so we can feed the ECDH output through HKDF
  // below — WebCrypto can't chain ECDH -> HKDF in a single deriveKey call.
  e2eeKeypair = await crypto.subtle.generateKey(
    { name: 'ECDH', namedCurve: 'P-256' }, false, ['deriveBits']
  );
  var rawPub = new Uint8Array(
    await crypto.subtle.exportKey('raw', e2eeKeypair.publicKey)
  );
  ws.send(JSON.stringify({
    type: 'e2ee_handshake',
    client_id: CLIENT_ID,
    pubkey: _b64FromBytes(rawPub)
  }));
  if (DEBUG_E2EE) console.log('[e2ee] handshake sent', CLIENT_ID);
}

async function completeHandshake(daemonPubB64) {
  var daemonPubBytes = _bytesFromB64(daemonPubB64);
  var daemonPub = await crypto.subtle.importKey(
    'raw', daemonPubBytes,
    { name: 'ECDH', namedCurve: 'P-256' }, false, []
  );
  // Step 1: ECDH -> 256-bit shared secret (raw bits)
  var sharedBits = await crypto.subtle.deriveBits(
    { name: 'ECDH', public: daemonPub },
    e2eeKeypair.privateKey,
    256
  );
  // Step 2: import as HKDF key material
  var hkdfKey = await crypto.subtle.importKey(
    'raw', sharedBits, { name: 'HKDF' }, false, ['deriveKey']
  );
  // Step 3: HKDF-SHA256 -> AES-GCM key (same info string as the Python side)
  e2eeKey = await crypto.subtle.deriveKey(
    {
      name: 'HKDF',
      hash: 'SHA-256',
      salt: new Uint8Array(0),
      info: new TextEncoder().encode('roboot-relay-e2ee-v1')
    },
    hkdfKey,
    { name: 'AES-GCM', length: 256 },
    false, ['encrypt', 'decrypt']
  );
  e2eeReady = true;
  if (DEBUG_E2EE) console.log('[e2ee] handshake complete');
  // Flush any app messages queued before the key was ready
  var q = preHandshakeQueue; preHandshakeQueue = [];
  for (var i = 0; i < q.length; i++) { secureSend(q[i]); }
}

async function secureSend(payload) {
  // Encrypt + ship a JSON app message. Silently queues if the handshake
  // hasn't finished yet so UI code never has to think about timing.
  if (!ws || ws.readyState !== 1) return;
  if (!e2eeReady) { preHandshakeQueue.push(payload); return; }
  var iv = new Uint8Array(12);
  crypto.getRandomValues(iv);
  var plaintext = new TextEncoder().encode(JSON.stringify(payload));
  var ctBuf = await crypto.subtle.encrypt(
    { name: 'AES-GCM', iv: iv }, e2eeKey, plaintext
  );
  ws.send(JSON.stringify({
    type: 'encrypted',
    client_id: CLIENT_ID,
    iv: _b64FromBytes(iv),
    ct: _b64FromBytes(new Uint8Array(ctBuf))
  }));
  if (DEBUG_E2EE) console.log('[e2ee] sent', payload.type, 'ct=' + ctBuf.byteLength + 'B');
}

async function secureDecrypt(envelope) {
  if (!e2eeReady) throw new Error('no key');
  var iv = _bytesFromB64(envelope.iv);
  var ct = _bytesFromB64(envelope.ct);
  var ptBuf = await crypto.subtle.decrypt(
    { name: 'AES-GCM', iv: iv }, e2eeKey, ct
  );
  return JSON.parse(new TextDecoder().decode(ptBuf));
}

// === Mobile Sidebar ===
function toggleSidebar() {
  var sb = document.getElementById('sidebar');
  var opening = !sb.classList.contains('open');
  sb.classList.toggle('open');
  document.getElementById('sidebar-backdrop').classList.toggle('open');
  // First open on mobile: subscribe to sessions now that the user actually
  // wants to see the list. Subsequent reconnects re-fetch automatically.
  if (opening && !sessionsWanted) {
    sessionsWanted = true;
    loadSessions();
  }
}
function closeSidebar() {
  document.getElementById('sidebar').classList.remove('open');
  document.getElementById('sidebar-backdrop').classList.remove('open');
}

// === Connection Status ===
function setConnStatus(text, online) {
  var el = document.getElementById('conn-status');
  el.textContent = text;
  el.className = 'conn-status show' + (online ? ' online' : '');
  if (online) {
    setTimeout(function() { el.classList.remove('show'); }, 2000);
  }
}

// === Markdown ===
function renderMD(t) {
  t = t.replace(/\`\`\`(\\w*)\\n([\\s\\S]*?)\`\`\`/g, '<pre><code>$2</code></pre>');
  t = t.replace(/\`([^\`]+)\`/g, '<code>$1</code>');
  t = t.replace(/\\*\\*(.+?)\\*\\*/g, '<strong>$1</strong>');
  t = t.replace(/!\\[([^\\]]*)\\]\\(([^)]+)\\)/g, '<img src="$2" alt="$1" style="max-width:100%;border-radius:8px;margin:8px 0;">');
  t = t.replace(/\\n/g, '<br>');
  return t;
}

function addChatMsg(role, text, tools) {
  var el = document.createElement('div');
  el.className = 'chat-msg ' + role;
  el.innerHTML = renderMD(text || '');
  if (tools && tools > 0) {
    var b = document.createElement('div');
    b.className = 'tools-badge';
    b.textContent = tools + ' tool call' + (tools > 1 ? 's' : '');
    el.appendChild(b);
  }
  var box = document.getElementById('chat-messages');
  box.appendChild(el);
  box.scrollTop = box.scrollHeight;
}

function addThinking() {
  if (thinkingEl) return;
  thinkingEl = document.createElement('div');
  thinkingEl.className = 'chat-msg thinking';
  thinkingEl.textContent = 'Thinking';
  var box = document.getElementById('chat-messages');
  box.appendChild(thinkingEl);
  box.scrollTop = box.scrollHeight;
}

function removeThinking() {
  if (thinkingEl) { thinkingEl.remove(); thinkingEl = null; }
}

function flushRender() {
  if (streamBubble && streamText) {
    streamBubble.innerHTML = renderMD(streamText);
    var box = document.getElementById('chat-messages');
    box.scrollTop = box.scrollHeight;
  }
  renderTimer = null;
}

function scheduleRender() {
  if (!renderTimer) {
    renderTimer = setTimeout(flushRender, 80);
  }
}

// === WebSocket ===
function connectWS() {
  setConnStatus('Connecting...', false);
  resetE2EE();

  try {
    ws = new WebSocket(RELAY_WS_URL);
  } catch(e) {
    setConnStatus('Connection failed: ' + e.message, false);
    return;
  }

  ws.onopen = async function() {
    connected = true;
    reconnectDelay = 1000;
    setConnStatus('Securing...', false);
    // Queue the app-level hello/session-load until the key is derived.
    secureSend({ type: 'client_hello' });
    // Lazy session list: only fetch if the sidebar is actually visible
    // (desktop always, mobile only after toggleSidebar has opened it once).
    // sessionsWanted persists across reconnects so the daemon re-subscribes.
    if (sessionsWanted || window.innerWidth > 768) {
      sessionsWanted = true;
      setTimeout(loadSessions, 500);
    }
    startHeartbeat();
    try {
      await startHandshake();
    } catch (err) {
      setConnStatus('Encryption setup failed: ' + err.message, false);
      try { ws.close(4001, 'e2ee_init_failed'); } catch(_) {}
    }
  };

  ws.onmessage = async function(e) {
    var frame;
    try { frame = JSON.parse(e.data); } catch (_) { return; }

    // Handshake reply: derive the shared key. App messages are queued
    // by secureSend() until this completes.
    if (frame.type === 'e2ee_handshake') {
      try {
        await completeHandshake(frame.pubkey);
        setConnStatus('Connected', true);
        document.getElementById('chat-send-btn').disabled = false;
      } catch (err) {
        setConnStatus('Handshake failed: ' + err.message, false);
        try { ws.close(4002, 'handshake_failed'); } catch(_) {}
      }
      return;
    }

    // Unencrypted error frames from the relay/daemon (e.g. handshake_failed).
    // client_id === our id means this error is ours; missing client_id is
    // treated as "addressed to us" too so the relay can still surface issues.
    if (frame.type === 'error' && !frame.ct) {
      if (!frame.client_id || frame.client_id === CLIENT_ID) {
        addChatMsg('bot', 'Error: ' + (frame.content || 'unknown'));
      }
      return;
    }

    // Heartbeat pong — sent by relay (plaintext, outside encryption envelope).
    if (frame.type === 'pong') {
      _lastPongAt = Date.now();
      return;
    }

    // Revoke notice — sent by relay DO (plaintext). Show lock screen and stop.
    if (frame.type === 'revoked') {
      _revoked = true;
      stopHeartbeat();
      showRevokedScreen(frame.reason || 'daemon_revoked');
      try { ws.close(); } catch(_e) {}
      return;
    }

    // Everything else MUST be encrypted.
    if (frame.type !== 'encrypted') {
      if (DEBUG_E2EE) console.warn('[e2ee] dropping unencrypted frame', frame.type);
      return;
    }
    // The relay broadcasts daemon frames to every client; skip envelopes
    // addressed to a different client rather than wasting work on decrypt.
    if (frame.client_id && frame.client_id !== CLIENT_ID) return;

    var data;
    try {
      data = await secureDecrypt(frame);
    } catch (err) {
      if (DEBUG_E2EE) console.warn('[e2ee] decrypt failed', err);
      return;
    }

    if (data.type === 'thinking') {
      removeThinking();
      addThinking();
      if (jarvisMode) setJarvisState('thinking');

    } else if (data.type === 'delta') {
      removeThinking();
      streamText += data.text;
      if (!streamBubble) {
        streamBubble = document.createElement('div');
        streamBubble.className = 'chat-msg bot';
        document.getElementById('chat-messages').appendChild(streamBubble);
      }
      scheduleRender();

    } else if (data.type === 'tool_start') {
      removeThinking();
      if (!streamBubble) {
        streamBubble = document.createElement('div');
        streamBubble.className = 'chat-msg bot';
        document.getElementById('chat-messages').appendChild(streamBubble);
      }
      streamText += '\\n\\ud83d\\udd27 ' + (data.name || 'tool') + '...\\n';
      streamBubble.innerHTML = renderMD(streamText);

    } else if (data.type === 'tool_end') {
      // continue

    } else if (data.type === 'done') {
      removeThinking();
      if (renderTimer) { clearTimeout(renderTimer); renderTimer = null; }
      if (streamBubble) {
        streamBubble.innerHTML = renderMD(data.content || streamText);
        if (data.tools_used > 0) {
          var b = document.createElement('div');
          b.className = 'tools-badge';
          b.textContent = data.tools_used + ' tool call' + (data.tools_used > 1 ? 's' : '');
          streamBubble.appendChild(b);
        }
        document.getElementById('chat-messages').scrollTop = 999999;
      }
      // JARVIS: speak the response
      if (jarvisMode && (data.content || streamText)) {
        queueSpeak(data.content || streamText);
      }
      // Update sessions list if included (after tool calls)
      if (data.sessions && data.sessions.length > 0) {
        sessions = data.sessions;
        renderSessionList();
      }
      streamBubble = null;
      streamText = '';
      document.getElementById('chat-send-btn').disabled = false;

    } else if (data.type === 'response') {
      removeThinking();
      addChatMsg('bot', data.content, data.tools_used);
      document.getElementById('chat-send-btn').disabled = false;

    } else if (data.type === 'error') {
      removeThinking();
      addChatMsg('bot', 'Error: ' + data.content);
      document.getElementById('chat-send-btn').disabled = false;

    } else if (data.type === 'sessions_list') {
      sessions = data.sessions || [];
      renderSessionList();

    } else if (data.type === 'session_content') {
      if (data.session_id === currentId) {
        var term = document.getElementById('terminal');
        var content = data.content || '';
        var lastLine = (typeof data.last_line === 'number') ? data.last_line : -1;
        var droppedPrefix = !!data.dropped_prefix;
        // iTerm2 scrollback rolled past our cursor — drop state, full refetch next tick.
        if (droppedPrefix) {
          delete lastLineBySession[currentId];
          term.textContent = '';
          refreshSession();
          return;
        }
        // Preserve scroll if the user is reading history; only pin to bottom
        // when they were already near it (within 40px).
        var nearBottom = (term.scrollHeight - term.scrollTop - term.clientHeight) < 40;
        var wasInitial = (lastLineBySession[currentId] === undefined);
        if (wasInitial) {
          // Initial fetch: replace.
          term.textContent = content || '(empty)';
        } else if (content) {
          // Incremental: append only non-empty delta. read_session_incremental
          // returns newline-terminated chunks so concatenation is safe.
          if (term.textContent === '(empty)') term.textContent = '';
          term.textContent += content;
        }
        if (lastLine >= 0) lastLineBySession[currentId] = lastLine;
        if (nearBottom) term.scrollTop = term.scrollHeight;
        // Check for waiting-for-approval prompts. Skip empty deltas so the
        // bar doesn't flicker away while a prompt is still on screen.
        if (wasInitial || content.length > 0) {
          checkForConfirmation(stripAnsi(content));
        }
      }

    } else if (data.type === 'session_sent') {
      if (data.ok) showToast('Sent');
      else showToast('Failed: ' + (data.error || ''));
    }
  };

  ws.onclose = function() {
    connected = false;
    ws = null;
    stopHeartbeat();
    resetE2EE();
    document.getElementById('chat-send-btn').disabled = true;
    if (_revoked) {
      setConnStatus('Access revoked', false);
      return;
    }
    setConnStatus('Disconnected. Reconnecting...', false);
    if (reconnectDelay <= 30000) {
      setTimeout(function() {
        reconnectDelay = Math.min(reconnectDelay * 2, 30000);
        connectWS();
      }, reconnectDelay);
    }
  };

  ws.onerror = function() {};
}

// === Heartbeat ===
function startHeartbeat() {
  stopHeartbeat();
  _lastPongAt = Date.now();
  _hbPingTimer = setInterval(function() {
    if (!ws || ws.readyState !== 1) return;
    try {
      ws.send(JSON.stringify({ type: 'ping', ts: Date.now() }));
    } catch (_e) {}
  }, HEARTBEAT_INTERVAL_MS);
  _hbWatchdog = setInterval(function() {
    if (!ws || ws.readyState !== 1) return;
    if (Date.now() - _lastPongAt > HEARTBEAT_TIMEOUT_MS) {
      // Zombie connection -- close; onclose will trigger reconnect.
      try { ws.close(); } catch (_e) {}
    }
  }, 5000);
}

function stopHeartbeat() {
  if (_hbPingTimer) { clearInterval(_hbPingTimer); _hbPingTimer = null; }
  if (_hbWatchdog) { clearInterval(_hbWatchdog); _hbWatchdog = null; }
}

// === Revoked screen ===
function showRevokedScreen(reason) {
  var body = document.body;
  body.innerHTML = ''
    + '<div style="display:flex;flex-direction:column;align-items:center;justify-content:center;'
    + 'height:100vh;padding:24px;text-align:center;background:var(--bg);color:var(--text);font-family:-apple-system,\\"PingFang SC\\",system-ui,sans-serif;">'
    + '<div style="width:72px;height:72px;border-radius:50%;background:rgba(248,81,73,0.15);'
    + 'display:flex;align-items:center;justify-content:center;font-size:36px;margin-bottom:20px;">\\ud83d\\udd12</div>'
    + '<h1 style="font-size:20px;font-weight:600;margin-bottom:10px;">访问已撤销</h1>'
    + '<p style="color:var(--text-dim);font-size:14px;max-width:320px;line-height:1.5;">'
    + '主机已撤销此配对链接。请向机主获取新的配对二维码。</p>'
    + '<p style="color:var(--text-dim);font-size:11px;margin-top:20px;">reason: ' + reason + '</p>'
    + '</div>';
}

function chatSend() {
  var inp = document.getElementById('txt-chat');
  var text = inp.value.trim();
  if (!text || !ws || ws.readyState !== 1) return;
  addChatMsg('user', text);
  secureSend({ type: 'chat', content: text });
  inp.value = ''; inp.style.height = 'auto';
  document.getElementById('chat-send-btn').disabled = true;
}

// === Toast ===
function showToast(msg) {
  var t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  setTimeout(function() { t.classList.remove('show'); }, 2000);
}

// === Textarea auto-resize + Enter to send ===
function setupTextarea(id, onSubmit) {
  var el = document.getElementById(id);
  el.addEventListener('input', function() {
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 100) + 'px';
  });
  el.addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      onSubmit();
    }
  });
  el.addEventListener('focus', function() {
    setTimeout(function() {
      el.scrollIntoView({ behavior: 'smooth', block: 'end' });
    }, 300);
  });
}

setupTextarea('txt-chat', chatSend);
setupTextarea('txt-session', sendToSession);

// === Viewport height fix (mobile) ===
function updateViewportHeight() {
  var vh = (window.visualViewport ? window.visualViewport.height : window.innerHeight) * 0.01;
  document.documentElement.style.setProperty('--vh', vh + 'px');
}
updateViewportHeight();
window.addEventListener('resize', updateViewportHeight);
window.addEventListener('orientationchange', updateViewportHeight);
if (window.visualViewport) {
  window.visualViewport.addEventListener('resize', updateViewportHeight);
}

// === Sessions ===
function showPanel(name) {
  document.querySelectorAll('.panel').forEach(function(p) { p.classList.remove('visible'); });
  document.getElementById('panel-' + name).classList.add('visible');
}

function showChat() {
  autoOn = false;
  stopAutoTimer();
  currentId = null;
  renderSessionList();
  document.getElementById('chat-tab').classList.add('active');
  showPanel('chat');
  closeSidebar();
}

function startAutoTimer() {
  if (autoTimer || !autoOn || !currentId || document.hidden) return;
  autoTimer = setInterval(refreshSession, 3000);
}
function stopAutoTimer() {
  if (autoTimer) { clearInterval(autoTimer); autoTimer = null; }
}

function loadSessions() {
  if (ws && ws.readyState === 1) {
    secureSend({ type: 'get_sessions' });
  }
}

function renderSessionList() {
  var el = document.getElementById('session-list');
  el.innerHTML = sessions.map(function(s) {
    var active = currentId === s.id;
    var label = s.project || s.name || s.id.substring(0,8);
    return '<div class="session-item' + (active ? ' active' : '') + '" onclick="selectSession(\\'' + s.id + '\\')">'
      + '<span class="dot" id="dot-' + s.id.replace(/[^a-zA-Z0-9]/g,'') + '"></span>'
      + '<span class="label">' + label + '</span></div>';
  }).join('');
  document.getElementById('session-count').textContent = sessions.length + ' sessions';
}

function selectSession(id) {
  stopAutoTimer();
  currentId = id;
  var s = sessions.find(function(x) { return x.id === id; });
  renderSessionList();
  document.getElementById('chat-tab').classList.remove('active');
  showPanel('session');
  document.getElementById('sv-title').textContent = s ? (s.project || s.name) : id;
  document.getElementById('sv-meta').textContent = s ? s.name : '';
  // Reset incremental cursor + clear terminal so first fetch is a full tail.
  delete lastLineBySession[id];
  document.getElementById('terminal').textContent = 'Loading...';
  document.getElementById('confirm-bar').classList.remove('visible');
  refreshSession();
  autoOn = true;
  startAutoTimer();
  document.getElementById('auto-btn').textContent = 'Auto \\u2713';
  closeSidebar();
}

function refreshSession() {
  if (!currentId || !ws || ws.readyState !== 1) return;
  var cursor = lastLineBySession[currentId];
  if (cursor === undefined) {
    secureSend({ type: 'read_session', session_id: currentId });
  } else {
    secureSend({ type: 'read_session', session_id: currentId, after_line: cursor });
  }
}

function toggleAuto() {
  var btn = document.getElementById('auto-btn');
  if (autoOn) {
    autoOn = false;
    stopAutoTimer();
    btn.textContent = 'Auto Refresh';
  } else {
    autoOn = true;
    refreshSession();
    startAutoTimer();
    btn.textContent = 'Auto \\u2713';
  }
}

// Pause polling when the tab/page is hidden (backgrounded, locked, switched
// away). On return, immediately refresh once then resume the timer.
document.addEventListener('visibilitychange', function() {
  if (document.hidden) {
    stopAutoTimer();
  } else if (autoOn && currentId) {
    refreshSession();
    startAutoTimer();
  }
});

function sendToSession() {
  var inp = document.getElementById('txt-session');
  var text = inp.value.trim();
  if (!text || !currentId || !ws || ws.readyState !== 1) return;
  inp.value = ''; inp.style.height = 'auto';
  secureSend({ type: 'send_session', session_id: currentId, text: text });
  showToast('Sent');
  setTimeout(refreshSession, 1500);
}

function checkForConfirmation(content) {
  var lines = content.split('\\n');
  var tail = lines.slice(-10).join('\\n');
  var bar = document.getElementById('confirm-bar');
  var promptEl = document.getElementById('confirm-prompt');
  var found = false;
  for (var i = 0; i < CONFIRM_PATTERNS.length; i++) {
    if (CONFIRM_PATTERNS[i].test(tail)) {
      found = true;
      for (var j = lines.length - 1; j >= Math.max(0, lines.length - 10); j--) {
        if (CONFIRM_PATTERNS[i].test(lines[j])) { promptEl.textContent = lines[j].trim(); break; }
      }
      break;
    }
  }
  bar.classList.toggle('visible', found);
  var dotId = currentId ? currentId.replace(/[^a-zA-Z0-9]/g,'') : '';
  var dot = document.getElementById('dot-' + dotId);
  if (dot) dot.classList.toggle('waiting', found);
}

function quickSend(text) {
  if (!currentId || !ws || ws.readyState !== 1) return;
  secureSend({ type: 'send_session', session_id: currentId, text: text });
  showToast(text === 'y' ? 'Allowed' : 'Denied');
  document.getElementById('confirm-bar').classList.remove('visible');
  setTimeout(refreshSession, 1500);
}

// === Voice Input (Web Speech API) ===
var micActive = false;
var recognition = null;
var jarvisMode = false;
var isSpeaking = false;
var speakQueue = [];

function toggleMic() {
  if (!window.isSecureContext && window.location.hostname !== 'localhost') {
    showToast('Voice requires HTTPS');
    return;
  }
  if (!('webkitSpeechRecognition' in window) && !('SpeechRecognition' in window)) {
    showToast('Browser does not support speech recognition, use Chrome');
    return;
  }
  if (micActive) { stopMic(); return; }
  startListening(false);
}

function startListening(isJarvis) {
  var SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SpeechRecognition) return;
  if (micActive) return;

  recognition = new SpeechRecognition();
  recognition.lang = 'zh-CN';
  recognition.continuous = false;
  recognition.interimResults = true;

  var btn = document.getElementById('mic-btn');
  var inp = document.getElementById('txt-chat');
  var gotResult = false;

  recognition.onstart = function() {
    micActive = true;
    gotResult = false;
    btn.style.background = 'var(--accent)';
    btn.style.color = 'white';
    if (isJarvis) setJarvisState('listening');
    else inp.placeholder = 'Listening...';
  };

  recognition.onresult = function(e) {
    var transcript = '';
    for (var i = e.resultIndex; i < e.results.length; i++) {
      transcript += e.results[i][0].transcript;
    }
    inp.value = transcript;
    if (e.results[e.results.length - 1].isFinal) {
      gotResult = true;
      micActive = false;
      btn.style.background = 'var(--surface2)';
      btn.style.color = 'var(--text-dim)';
      recognition = null;
      if (transcript.trim()) chatSend();
    }
  };

  recognition.onerror = function(e) {
    micActive = false;
    btn.style.background = 'var(--surface2)';
    btn.style.color = 'var(--text-dim)';
    recognition = null;
    if (e.error !== 'no-speech' && e.error !== 'aborted') {
      showToast('Voice: ' + e.error);
    }
    if (jarvisMode) {
      setTimeout(function() { if (jarvisMode) startListening(true); }, 800);
    }
  };

  recognition.onend = function() {
    micActive = false;
    btn.style.background = 'var(--surface2)';
    btn.style.color = 'var(--text-dim)';
    inp.placeholder = 'Say something...';
    recognition = null;
    if (jarvisMode && !gotResult) {
      setTimeout(function() { if (jarvisMode) startListening(true); }, 300);
    }
  };

  try { recognition.start(); } catch(e) {
    micActive = false;
    if (jarvisMode) {
      setTimeout(function() { if (jarvisMode) startListening(true); }, 1000);
    }
  }
}

function stopMic() {
  micActive = false;
  document.getElementById('mic-btn').style.background = 'var(--surface2)';
  document.getElementById('mic-btn').style.color = 'var(--text-dim)';
  document.getElementById('txt-chat').placeholder = 'Say something...';
  if (recognition) { try { recognition.stop(); } catch(e) {} recognition = null; }
}

// === JARVIS Mode ===
function toggleJarvis() {
  if (!jarvisMode && !window.isSecureContext && window.location.hostname !== 'localhost') {
    showToast('JARVIS requires HTTPS');
    return;
  }
  jarvisMode = !jarvisMode;
  var btn = document.getElementById('jarvis-btn');
  var orb = document.getElementById('jarvis-orb');

  if (jarvisMode) {
    btn.style.background = 'var(--accent)';
    btn.style.color = 'white';
    btn.textContent = 'Exit JARVIS';
    orb.style.display = 'block';
    setJarvisState('listening');
    startListening(true);
  } else {
    btn.style.background = 'var(--surface2)';
    btn.style.color = 'var(--text)';
    btn.textContent = 'JARVIS';
    orb.style.display = 'none';
    stopMic();
    stopSpeaking();
  }
}

function setJarvisState(state) {
  var orbEl = document.getElementById('orb');
  var statusEl = document.getElementById('jarvis-status');
  if (state === 'listening') {
    orbEl.style.background = 'radial-gradient(circle,rgba(63,185,80,0.6),rgba(63,185,80,0.15))';
    orbEl.style.boxShadow = '0 0 40px rgba(63,185,80,0.3)';
    statusEl.textContent = 'Listening...';
  } else if (state === 'thinking') {
    orbEl.style.background = 'radial-gradient(circle,rgba(233,69,96,0.7),rgba(83,52,131,0.3))';
    orbEl.style.boxShadow = '0 0 50px rgba(233,69,96,0.4)';
    statusEl.textContent = 'Thinking...';
  } else if (state === 'speaking') {
    orbEl.style.background = 'radial-gradient(circle,rgba(56,132,244,0.7),rgba(83,52,131,0.3))';
    orbEl.style.boxShadow = '0 0 50px rgba(56,132,244,0.4)';
    statusEl.textContent = 'Speaking...';
  }
}

// === Browser TTS (speechSynthesis) ===
function queueSpeak(text) {
  if (!text) return;
  // Extract spoken part: lines starting with "> " or first sentence
  var lines = text.split('\\n');
  var spoken = '';
  for (var i = 0; i < lines.length; i++) {
    if (lines[i].startsWith('> ')) {
      spoken += lines[i].substring(2) + ' ';
    }
  }
  if (!spoken) {
    // Fallback: first 200 chars
    spoken = text.replace(/[#*\`\\[\\]()]/g, '').substring(0, 200);
  }
  spoken = spoken.trim();
  if (!spoken) return;

  speakQueue.push(spoken);
  if (!isSpeaking) drainSpeakQueue();
}

function drainSpeakQueue() {
  if (speakQueue.length === 0) {
    isSpeaking = false;
    if (jarvisMode) {
      setTimeout(function() { if (jarvisMode && !micActive) startListening(true); }, 300);
    }
    return;
  }
  isSpeaking = true;
  if (jarvisMode) setJarvisState('speaking');
  var text = speakQueue.shift();

  var utterance = new SpeechSynthesisUtterance(text);
  utterance.lang = 'zh-CN';
  utterance.rate = 1.1;
  utterance.onend = function() { drainSpeakQueue(); };
  utterance.onerror = function() { drainSpeakQueue(); };
  speechSynthesis.speak(utterance);
}

function stopSpeaking() {
  speakQueue = [];
  isSpeaking = false;
  speechSynthesis.cancel();
}

// === Start ===
connectWS();
document.getElementById('txt-chat').focus();
</script>
</body>
</html>`;
