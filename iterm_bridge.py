"""iTerm2 Python API bridge — fast, real-time session access."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

import iterm2


# --- ANSI rendering helpers ------------------------------------------------
# Walk LineContents cells and emit SGR escape sequences so the frontend (or
# any ANSI-aware consumer) can re-render colors/styles. ansi_up on the web
# side turns these into HTML spans.

_SGR_RESET = "\x1b[0m"


def _fg_param(color) -> str:
    """Return SGR parameter for foreground color, '' for default."""
    if color is None:
        return ""
    if color.is_rgb:
        c = color.rgb
        return f"38;2;{c.red};{c.green};{c.blue}"
    if color.is_standard:
        n = color.standard
        if n < 8:
            return str(30 + n)
        if n < 16:
            return str(90 + (n - 8))
        return f"38;5;{n}"
    return ""


def _bg_param(color) -> str:
    if color is None:
        return ""
    if color.is_rgb:
        c = color.rgb
        return f"48;2;{c.red};{c.green};{c.blue}"
    if color.is_standard:
        n = color.standard
        if n < 8:
            return str(40 + n)
        if n < 16:
            return str(100 + (n - 8))
        return f"48;5;{n}"
    return ""


def _style_sgr(style) -> str:
    """Build an SGR escape for a CellStyle (empty string = default/no styling)."""
    if style is None:
        return ""
    params: list[str] = []
    fg = _fg_param(style.fg_color)
    bg = _bg_param(style.bg_color)
    if fg:
        params.append(fg)
    if bg:
        params.append(bg)
    if style.bold:
        params.append("1")
    if style.faint:
        params.append("2")
    if style.italic:
        params.append("3")
    if style.underline:
        params.append("4")
    if not params:
        return ""
    return f"\x1b[{';'.join(params)}m"


def _style_key(style) -> tuple:
    """Hashable key for change detection between adjacent cells."""
    if style is None:
        return ()
    return (
        _fg_param(style.fg_color),
        _bg_param(style.bg_color),
        style.bold, style.faint, style.italic, style.underline,
    )


def _render_line_ansi(line) -> str:
    """Render a LineContents to an ANSI-escaped string."""
    parts: list[str] = []
    last_key = None
    x = 0
    while True:
        style = line.style_at(x)
        if style is None:
            break
        key = _style_key(style)
        if key != last_key:
            parts.append(_SGR_RESET)
            sgr = _style_sgr(style)
            if sgr:
                parts.append(sgr)
            last_key = key
        parts.append(line.string_at(x))
        x += 1
    parts.append(_SGR_RESET)
    return "".join(parts).rstrip()


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

    async def read_session_ansi(self, session_id: str, num_lines: int = 150) -> str:
        """Like read_session but emits ANSI SGR escapes for cell colors/styles.
        Frontend can render via ansi_up or similar.
        """
        app = await self._get_app()
        session = app.get_session_by_id(session_id)
        if not session:
            return f"Session {session_id} not found"

        try:
            li = await session.async_get_line_info()
            total = li.scrollback_buffer_height + li.mutable_area_height
            start = max(li.overflow, li.overflow + total - num_lines)
            count = min(num_lines, total)
            lines = await session.async_get_contents(start, count)
            out_lines: list[str] = []
            for line in lines:
                if not line.string.strip():
                    continue
                out_lines.append(_render_line_ansi(line))
            return "\n".join(out_lines)
        except Exception as e:
            return f"Read error: {e}"

    async def read_session_incremental(
        self,
        session_id: str,
        after_line: int | None = None,
        num_lines_initial: int = 1000,
        color: bool = False,
    ) -> dict:
        """Incremental read.

        - after_line=None  -> initial fetch: return up to num_lines_initial most recent lines.
        - after_line=<int> -> delta fetch:   return all lines with absolute line number > after_line.

        Returns:
          {
            "content":        str,   # rendered lines joined by "\n" (plain or ANSI-escaped if color=True)
            "last_line":      int,   # absolute line number of the last line returned; -1 if empty
            "overflow":       int,   # iTerm2's current overflow counter
            "dropped_prefix": bool,  # True if after_line < overflow (caller's range partly gone)
            "error":          str,   # only if session not found / API errored; other fields may be omitted
          }
        """
        app = await self._get_app()
        session = app.get_session_by_id(session_id)
        if not session:
            return {"error": f"Session {session_id} not found"}

        try:
            li = await session.async_get_line_info()
            overflow = li.overflow
            total = li.scrollback_buffer_height + li.mutable_area_height
            # Absolute line numbers valid range: [overflow, overflow + total - 1].
            last_valid = overflow + total - 1

            dropped_prefix = False
            do_initial = after_line is None

            if not do_initial:
                if after_line < overflow:
                    # Caller's anchor is already gone from the buffer.
                    dropped_prefix = True
                    do_initial = True
                elif after_line >= last_valid:
                    # Nothing new.
                    return {
                        "content": "",
                        "last_line": after_line,
                        "overflow": overflow,
                        "dropped_prefix": False,
                    }

            if do_initial:
                # Up to num_lines_initial most recent lines.
                first_line = overflow + max(0, total - num_lines_initial)
                count = (overflow + total) - first_line
            else:
                first_line = after_line + 1
                count = (overflow + total) - first_line

            if count <= 0:
                return {
                    "content": "",
                    "last_line": -1 if do_initial else after_line,
                    "overflow": overflow,
                    "dropped_prefix": dropped_prefix,
                }

            lines = await session.async_get_contents(first_line, count)
            rendered: list[str] = []
            for line in lines:
                if color:
                    rendered.append(_render_line_ansi(line))
                else:
                    rendered.append(line.string)

            # Actual last absolute line number returned.
            actual_count = len(rendered)
            if actual_count == 0:
                return {
                    "content": "",
                    "last_line": -1 if do_initial else after_line,
                    "overflow": overflow,
                    "dropped_prefix": dropped_prefix,
                }
            last_line = first_line + actual_count - 1

            return {
                "content": "\n".join(rendered),
                "last_line": last_line,
                "overflow": overflow,
                "dropped_prefix": dropped_prefix,
            }
        except Exception as e:
            return {"error": f"Read error: {e}"}

    async def send_text(self, session_id: str, text: str) -> str:
        """Send text to a session (like typing)."""
        app = await self._get_app()
        session = app.get_session_by_id(session_id)
        if not session:
            return f"Session {session_id} not found"

        try:
            # Use \r (CR) not \n (LF): TUI apps like Claude Code treat \n as
            # "insert newline in input" while \r is "press Enter / submit".
            # Regular shells accept either, so \r is the safer default.
            await session.async_send_text(text + "\r")
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
