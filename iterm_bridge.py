"""iTerm2 Python API bridge — fast, real-time session access."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import iterm2


@dataclass
class SessionInfo:
    session_id: str
    name: str
    pid: str
    tty: str
    cwd: str
    project: str


class ITermBridge:
    """Manages a persistent connection to iTerm2's Python API."""

    def __init__(self):
        self._conn: iterm2.Connection | None = None
        self._lock = asyncio.Lock()

    async def _ensure_connected(self) -> iterm2.Connection:
        async with self._lock:
            if self._conn is None:
                self._conn = await iterm2.Connection.async_create()
            return self._conn

    async def _get_app(self) -> iterm2.App:
        conn = await self._ensure_connected()
        return await iterm2.async_get_app(conn)

    async def list_sessions(self) -> list[SessionInfo]:
        """List all iTerm2 sessions with Claude Code running."""
        try:
            app = await self._get_app()
        except Exception:
            # Reconnect on failure
            self._conn = None
            app = await self._get_app()

        results = []
        for w in app.terminal_windows:
            for t in w.tabs:
                for s in t.sessions:
                    name = s.name or ""
                    # Get variable info
                    try:
                        pid = str(await s.async_get_variable("jobPid") or "")
                        tty = str(await s.async_get_variable("tty") or "")
                        cwd = str(await s.async_get_variable("path") or "")
                    except Exception:
                        pid, tty, cwd = "", "", ""

                    project = cwd.rstrip("/").split("/")[-1] if cwd else name
                    results.append(SessionInfo(
                        session_id=s.session_id,
                        name=name,
                        pid=pid,
                        tty=tty,
                        cwd=cwd,
                        project=project or s.session_id[:8],
                    ))
        return results

    async def read_session(self, session_id: str, num_lines: int = 150) -> str:
        """Read the last N lines from a session. Fast — no AppleScript."""
        app = await self._get_app()
        session = app.get_session_by_id(session_id)
        if not session:
            return f"Session {session_id} not found"

        try:
            li = await session.async_get_line_info()
            total = li.scrollback_buffer_height + li.mutable_area_height
            # overflow = lines that scrolled out of the buffer
            # async_get_contents uses absolute coords starting from overflow
            start = max(li.overflow, li.overflow + total - num_lines)
            count = min(num_lines, total)
            lines = await session.async_get_contents(start, count)
            text_lines = [l.string for l in lines if l.string.strip()]
            return "\n".join(text_lines)
        except Exception as e:
            return f"Read error: {e}"

    async def send_text(self, session_id: str, text: str) -> str:
        """Send text to a session (like typing)."""
        app = await self._get_app()
        session = app.get_session_by_id(session_id)
        if not session:
            return f"Session {session_id} not found"

        try:
            await session.async_send_text(text + "\n")
            return "sent"
        except Exception as e:
            return f"Send error: {e}"

    async def create_session(self, directory: str, initial_prompt: str = "") -> str:
        """Create a new iTerm2 tab and start Claude Code."""
        try:
            conn = await self._ensure_connected()
            app = await iterm2.async_get_app(conn)
            window = app.current_terminal_window
            if not window:
                return "没有打开的 iTerm2 窗口"

            tab = await window.async_create_tab()
            session = tab.current_session

            cmd = f"cd {directory} && claude"
            if initial_prompt:
                escaped = initial_prompt.replace('"', '\\"')
                cmd = f'cd {directory} && claude "{escaped}"'

            await session.async_send_text(cmd + "\n")
            await asyncio.sleep(2)
            return f"已在 iTerm2 新 tab 启动 Claude Code，目录: {directory}"
        except Exception as e:
            return f"创建失败: {e}"

    async def close(self):
        self._conn = None


# Singleton
bridge = ITermBridge()
