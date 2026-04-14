"""Relay client -- connects Roboot to the central relay for remote access."""

from __future__ import annotations

import asyncio
import json
import secrets
import time
import uuid

import websockets

# Relay protocol:
# 1. Daemon connects to wss://relay.coordbound.com/ws/daemon/{session_id}
# 2. Sends {"type": "daemon_hello", "version": "1.0"}
# 3. Receives messages from clients, forwards to local Arcana session
# 4. Sends Arcana responses back through relay to clients

OFFICIAL_RELAY = "wss://relay.coordbound.com"


class RelayClient:
    """Maintains a WebSocket connection to a relay server, forwarding
    messages between remote mobile clients and the local Arcana runtime."""

    def __init__(self, relay_url: str, runtime, build_personality, get_name, token_ttl: int = 1800):
        self.relay_url = relay_url.rstrip("/")
        self.runtime = runtime
        self.build_personality = build_personality
        self.get_name = get_name
        self.session_id = str(uuid.uuid4())
        # 32-byte (256-bit) cryptographically random token, hex-encoded = 64 chars
        self.token = secrets.token_hex(32)
        self.token_created_at: float = time.time()
        self.token_ttl: int = token_ttl  # seconds, default 30 minutes
        self.ws = None
        self._loop = None  # Set when start() runs; used for cross-thread close
        self.running = False
        # Map of client_id -> Arcana ChatSession
        self.chat_sessions: dict = {}

    @property
    def pairing_url(self) -> str:
        """URL a mobile client should open to pair with this daemon.
        Contains both session_id and cryptographic token for authentication."""
        base = self.relay_url.replace("wss://", "https://").replace(
            "ws://", "http://"
        )
        return f"{base}/pair/{self.session_id}?token={self.token}"

    @property
    def token_expired(self) -> bool:
        """Check if the current token has exceeded its TTL."""
        return time.time() > self.token_created_at + self.token_ttl

    def rotate_token(self):
        """Generate new session_id and token, forcing a reconnect.

        Safe to call from any thread. The existing auto-reconnect loop in
        start() will pick up the new credentials after the old WebSocket closes.
        """
        self.session_id = str(uuid.uuid4())
        self.token = secrets.token_hex(32)
        self.token_created_at = time.time()
        # Clear chat sessions since clients will need to re-pair
        self.chat_sessions.clear()
        # Close existing WebSocket so the reconnect loop picks up new creds.
        # Grab a local reference to avoid races, then set self.ws = None so
        # _send_to_client() stops using the old socket immediately.
        ws = self.ws
        if ws is not None:
            self.ws = None
            # Schedule ws.close() on the relay's own event loop (which runs
            # in a separate thread).  This is safe to call from any thread.
            if self._loop is not None:
                asyncio.run_coroutine_threadsafe(ws.close(), self._loop)
        print("[relay] Token rotated — new pairing URL generated")

    async def start(self):
        """Start relay connection with auto-reconnect and exponential backoff."""
        self._loop = asyncio.get_event_loop()
        self.running = True
        backoff = 1
        while self.running:
            try:
                await self._connect()
                backoff = 1  # Reset on successful connection
            except Exception as e:
                print(f"[relay] Connection error: {e}")
                if self.running:
                    print(f"[relay] Reconnecting in {backoff}s...")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)

    async def _connect(self):
        """Establish WebSocket connection to relay and handle messages."""
        # Auto-rotate expired tokens before connecting
        if self.token_expired:
            self.rotate_token()
        url = f"{self.relay_url}/ws/daemon/{self.session_id}?token={self.token}"
        async with websockets.connect(url) as ws:
            self.ws = ws
            # Register with relay
            await ws.send(
                json.dumps(
                    {
                        "type": "daemon_hello",
                        "version": "1.0",
                        "name": self.get_name(),
                    }
                )
            )
            print("[relay] Connected to relay")

            # Process incoming messages until disconnect
            async for raw_message in ws:
                await self._handle_message(raw_message)

    async def _handle_message(self, raw: str):
        """Handle a message forwarded from a mobile client via the relay."""
        try:
            msg = json.loads(raw)
            msg_type = msg.get("type")
            client_id = msg.get("client_id", "default")

            if msg_type == "client_hello":
                await self._on_client_hello(client_id)
            elif msg_type == "chat":
                await self._on_chat(client_id, msg)
            elif msg_type == "get_sessions":
                await self._on_get_sessions(client_id)
            elif msg_type == "read_session":
                await self._on_read_session(client_id, msg)
            elif msg_type == "send_session":
                await self._on_send_session(client_id, msg)
            elif msg_type == "client_disconnect":
                self.chat_sessions.pop(client_id, None)

        except Exception as e:
            print(f"[relay] Error handling message: {e}")

    async def _on_client_hello(self, client_id: str):
        """A new client connected -- create a chat session and send welcome."""
        personality = self.build_personality()
        session = self.runtime.create_chat_session(system_prompt=personality)
        self.chat_sessions[client_id] = session

        name = self.get_name()
        await self._send_to_client(
            client_id,
            {
                "type": "response",
                "content": f"Hey, I'm {name}. Remote relay connection established!",
            },
        )

    async def _on_chat(self, client_id: str, msg: dict):
        """Process a chat message from a remote client, stream the response back."""
        user_text = msg.get("content", "").strip()
        if not user_text:
            return

        session = self.chat_sessions.get(client_id)
        if not session:
            personality = self.build_personality()
            session = self.runtime.create_chat_session(system_prompt=personality)
            self.chat_sessions[client_id] = session

        # Thinking indicator
        await self._send_to_client(client_id, {"type": "thinking"})

        # Stream response
        full_text = ""
        tools_used = 0

        async for event in session.stream(user_text):
            etype = str(event.event_type)

            if "LLM_CHUNK" in etype and event.content:
                full_text += event.content
                await self._send_to_client(
                    client_id, {"type": "delta", "text": event.content}
                )
            elif "TOOL_START" in etype or "TOOL_CALL_START" in etype:
                tools_used += 1
                await self._send_to_client(
                    client_id,
                    {"type": "tool_start", "name": event.tool_name or ""},
                )
            elif "TOOL_END" in etype or "TOOL_RESULT" in etype:
                await self._send_to_client(
                    client_id,
                    {"type": "tool_end", "name": event.tool_name or ""},
                )
            elif "RUN_COMPLETE" in etype and event.content:
                if not full_text:
                    full_text = event.content

        # Send done (include sessions after tool use, matching server.py)
        resp_data = {
            "type": "done",
            "content": full_text,
            "tools_used": tools_used,
        }

        if tools_used > 0:
            try:
                from iterm_bridge import bridge

                all_sessions = await bridge.list_sessions()
                resp_data["sessions"] = [
                    {"id": s.session_id, "project": s.project, "name": s.name}
                    for s in all_sessions
                ]
            except Exception:
                pass

        await self._send_to_client(client_id, resp_data)

    async def _on_get_sessions(self, client_id: str):
        """Return the list of iTerm2 Claude Code sessions."""
        try:
            from iterm_bridge import bridge

            sessions = await bridge.list_sessions()
            await self._send_to_client(
                client_id,
                {
                    "type": "sessions_list",
                    "sessions": [
                        {
                            "id": s.session_id,
                            "project": s.project,
                            "name": s.name,
                        }
                        for s in sessions
                    ],
                },
            )
        except Exception as e:
            await self._send_to_client(
                client_id, {"type": "sessions_list", "sessions": [], "error": str(e)}
            )

    async def _on_read_session(self, client_id: str, msg: dict):
        """Read content from a specific iTerm2 session."""
        session_id = msg.get("session_id", "")
        try:
            from iterm_bridge import bridge

            content = await bridge.read_session(session_id, num_lines=150)
            await self._send_to_client(
                client_id,
                {"type": "session_content", "session_id": session_id, "content": content},
            )
        except Exception as e:
            await self._send_to_client(
                client_id,
                {"type": "session_content", "session_id": session_id, "content": f"Error: {e}"},
            )

    async def _on_send_session(self, client_id: str, msg: dict):
        """Send text to a specific iTerm2 session."""
        session_id = msg.get("session_id", "")
        text = msg.get("text", "")
        try:
            from iterm_bridge import bridge

            await bridge.send_text(session_id, text)
            await self._send_to_client(
                client_id, {"type": "session_sent", "session_id": session_id, "ok": True}
            )
        except Exception as e:
            await self._send_to_client(
                client_id, {"type": "session_sent", "session_id": session_id, "ok": False, "error": str(e)}
            )

    async def _send_to_client(self, client_id: str, data: dict):
        """Send a message to a specific client through the relay."""
        if self.ws:
            data["client_id"] = client_id
            await self.ws.send(json.dumps(data))

    def stop(self):
        """Stop the relay connection gracefully."""
        self.running = False
        if self.ws:
            asyncio.create_task(self.ws.close())
