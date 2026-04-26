"""Approval gate for agent tool calls.

Hooks into Arcana's `ToolGateway.confirmation_callback` so that any tool
flagged `requires_confirmation=True` (or `side_effect=WRITE`) routes through
this module before execution. Today only `shell` is wired up — the gate
inspects the `command` argument against a danger-pattern list and asks the
user to approve before running.

Modes (env `ROBOOT_TOOL_APPROVAL`):
    off     — bypass entirely; callback always allows (default).
    log     — bypass execution but record matched dangerous calls to
              `.tool_audit/<ts>-<tool>.json` for after-the-fact review.
    confirm — broadcast a `tool_approval` frame to every registered
              broadcaster (local console + paired mobile + Telegram) and
              await a `tool_approval_decision` reply. No reply within
              `timeout_s` counts as REJECTED.

Allowlist (`~/.roboot/tool_allowlist.json`): per-machine list of tool-name +
command-prefix tuples that auto-approve. Lets the user whitelist routine
calls (e.g. `git status`) so the modal doesn't fire for noise.

Wiring:
    # in server.py startup
    runtime._tool_gateway.confirmation_callback = tool_guard.confirmation_callback
    tool_guard.register_broadcaster(_broadcast_to_local_ws)

    # in tools/shell.py decorator
    @arcana.tool(..., requires_confirmation=True)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import unicodedata
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

AUDIT_DIR = Path(__file__).parent / ".tool_audit"
ALLOWLIST_PATH = Path.home() / ".roboot" / "tool_allowlist.json"
MAX_ARGS_BYTES = 2048
DEFAULT_TIMEOUT_S = 30.0


class Mode(str, Enum):
    OFF = "off"
    LOG = "log"
    CONFIRM = "confirm"


class Decision(str, Enum):
    AUTO = "auto"          # mode=OFF, no danger pattern, or allowlisted
    LOGGED = "logged"      # mode=LOG (or CONFIRM degraded) → audit + allow
    APPROVED = "approved"  # mode=CONFIRM → user clicked allow
    REJECTED = "rejected"  # mode=CONFIRM denied / timed out / oversize


def get_mode() -> Mode:
    raw = os.environ.get("ROBOOT_TOOL_APPROVAL", "off").strip().lower()
    try:
        return Mode(raw)
    except ValueError:
        logger.warning("unknown ROBOOT_TOOL_APPROVAL=%r, defaulting to off", raw)
        return Mode.OFF


# ---------------------------------------------------------------------------
# Danger detection (curated subset of hermes-agent's pattern list, plus a few
# Roboot-specific paths). Tuples are (regex, human-readable reason).
# ---------------------------------------------------------------------------

DANGEROUS_PATTERNS: list[tuple[str, str]] = [
    # ─── rm / filesystem destruction ───
    (r"\brm\s+(-[^\s]*\s+)*/", "rm targeting root path"),
    (r"\brm\s+-[^\s]*r", "recursive rm"),
    (r"\brm\s+--recursive\b", "recursive rm (long flag)"),
    (r"\bmkfs\b", "format filesystem"),
    (r"\bdd\s+.*\bif=", "dd disk copy"),
    (r">\s*/dev/sd", "write to block device"),
    (r"\bfind\b[^|;&]*\s-delete\b", "find -delete"),
    (r"\bfind\b[^|;&]*-exec\s+(\S+/)?rm\b", "find -exec rm"),
    (r"\bxargs\s+[^|;&]*\brm\b", "xargs rm"),
    # ─── permission / privilege ───
    (r"\bchmod\s+(-[^\s]*\s+)*(777|666|o\+w|a\+w)\b", "world-writable chmod"),
    (r"\b(sudo|doas|pkexec|run0)\b", "privilege escalation"),
    # ─── pipe-to-shell / interpreter (prompt-injection landing pad) ───
    # Optional wrapper (sudo|nohup|env|exec) before the shell name; covers
    # bash/sh/zsh/ksh/csh/tcsh/dash/fish via the (ba|z|k|tc|c|da|fi)?sh form.
    (
        r"\b(curl|wget|fetch|http)\b[^|]*\|\s*"
        r"(sudo\s+|nohup\s+|env\s+|exec\s+)?"
        r"(ba|z|k|tc|c|da|fi)?sh\b",
        "pipe remote payload to shell",
    ),
    (
        r"\b(curl|wget|fetch|http)\b[^|]*\|\s*"
        r"(sudo\s+|nohup\s+|env\s+|exec\s+)?"
        r"(python[23]?|perl|ruby|node|php|lua|deno)\b",
        "pipe remote payload to interpreter",
    ),
    (r"<\(\s*(curl|wget|fetch|http)\b", "process-substitution remote payload"),
    (r"\beval\s+[\"']?\$\(", "eval of subshell output"),
    (r"(?:^|[;&|])\s*\.\s+<\(", "dot-source process substitution"),
    (r"\bsource\s+<\(", "source process substitution"),
    # ─── interpreter inline payload ───
    # Allow `-c"x"` (no whitespace before quote) and `-c 'x'`.
    (
        r"\b(python[23]?|perl|ruby|node|php|deno|bash|sh|zsh|ksh)\s*-[ec][\"'\s]",
        "interpreter -e/-c payload",
    ),
    # `[^|;&]*` allows `python3 - <<EOF` (the `-` between python and `<<`).
    (
        r"\b(python[23]?|perl|ruby|node|php)[^|;&\n]*<<",
        "interpreter heredoc payload",
    ),
    # ─── system config / sensitive paths ───
    (r">>?\s*/etc/", "overwrite /etc"),
    (r"\b(cp|mv|install)\b[^|;&]*\s/etc/", "place file in /etc"),
    (r"\btee\b\s+(-[^\s]*\s+)*/etc/", "tee into /etc"),
    (r"\bsed\s+-[^\s]*i[^|;&]*\s/etc/", "in-place edit of /etc"),
    # ─── credential dirs (~/.ssh and absolute /Users/x/.ssh both) ───
    (
        r"(?:~|\$HOME|\$\{HOME\}|/Users/[^/\s]+|/home/[^/\s]+)"
        r"/\.(ssh|aws|gnupg|kube|config/gcloud)(?:/|\b)",
        "touch credential dir",
    ),
    (
        r"\btee\b\s+(-[^\s]*\s+)*"
        r"(?:~|\$HOME|/Users/[^/\s]+|/home/[^/\s]+)"
        r"/\.(ssh|aws|gnupg)",
        "tee into credential dir",
    ),
    # ─── Roboot at-rest secrets, anchored to path-like context ───
    (r"(?:^|[\s/'\"=])(?:\./)?config\.yaml(?:\s|$|['\"])", "touch repo config.yaml"),
    (r"(?:^|[\s/'\"=])\.identity/", "touch .identity/ (daemon key)"),
    (r"(?:^|[\s/'\"=])\.faces/", "touch .faces/ (face DB)"),
    (
        r"(?:^|[\s/'\"=])\.chat_history\.db(?:\s|$|['\"])",
        "touch chat history db",
    ),
    # ─── git destructive ───
    (r"\bgit\s+reset\s+--hard\b", "git reset --hard"),
    (r"\bgit\s+push\b[^|;&]*--force\b", "git force push"),
    (r"\bgit\s+push\b[^|;&]*\s-f\b", "git force push (-f)"),
    (r"\bgit\s+clean\s+-[^\s]*f", "git clean -f"),
    (r"\bgit\s+branch\s+-D\b", "git branch -D"),
    # ─── reverse shell / network exec ───
    (r"\bnc\s+(-[^\s]*\s+)*-[el]", "netcat -e/-l (reverse shell)"),
    (r"/dev/tcp/", "/dev/tcp reverse shell"),
    # ─── macOS persistence / footguns ───
    (r"\bosascript\s+(-[^\s]+\s+)*-e\b", "osascript inline payload"),
    (r"\blaunchctl\s+(load|bootstrap|submit)\b", "launchctl daemon install"),
    (r"\bdefaults\s+write\b", "defaults write (persistence)"),
    # ─── mass kill / fork bomb ───
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "fork bomb"),
    (r"\bkill\s+-9\s+-1\b", "kill all processes"),
    (r"\bpkill\s+-9\b", "force kill processes"),
]

_COMPILED_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(pat, re.IGNORECASE), reason) for pat, reason in DANGEROUS_PATTERNS
]


_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")


def _normalize(text: str) -> str:
    """NFKC + ANSI strip + null-byte strip — defeats trivial obfuscation."""
    text = _ANSI_RE.sub("", text)
    text = text.replace("\x00", "")
    return unicodedata.normalize("NFKC", text)


MAX_DETECT_INPUT_BYTES = 16 * 1024


def detect_dangerous(command: str) -> str | None:
    """Return a human-readable reason string if the command matches a danger
    pattern, else None.

    Hard-caps input length to bound regex cost (ReDoS guard). A legitimately
    long command is still capped to the first ~16 KB; the OVERSIZE check in
    `gate()` handles the actual rejection on the modal path.
    """
    if not command:
        return None
    if len(command) > MAX_DETECT_INPUT_BYTES:
        return "command exceeds size cap"
    norm = _normalize(command)
    for pat, reason in _COMPILED_PATTERNS:
        if pat.search(norm):
            return reason
    return None


# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------


_allowlist_cache: dict[str, Any] = {"mtime": 0.0, "entries": []}


def _load_allowlist() -> list[dict[str, str]]:
    """Lazy-load allowlist; reload only if mtime changes.

    Format: list of objects {"tool": "shell", "prefix": "git status"}.
    A `prefix` match means the tool's primary command argument starts with
    that string (after leading whitespace strip).
    """
    try:
        mtime = ALLOWLIST_PATH.stat().st_mtime
    except FileNotFoundError:
        _allowlist_cache["mtime"] = 0.0
        _allowlist_cache["entries"] = []
        return []
    if mtime == _allowlist_cache["mtime"]:
        return _allowlist_cache["entries"]
    try:
        raw = json.loads(ALLOWLIST_PATH.read_text())
        if not isinstance(raw, list):
            logger.warning("tool_allowlist.json must be a list, ignoring")
            raw = []
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("failed to read tool_allowlist.json: %s", e)
        raw = []
    _allowlist_cache["mtime"] = mtime
    _allowlist_cache["entries"] = raw
    return raw


def _primary_text(tool_name: str, args: dict) -> str:
    """The main user-supplied string for a given tool — the thing we match
    danger patterns and allowlist prefixes against."""
    if tool_name == "shell":
        return str(args.get("command") or "")
    if tool_name == "send_to_session":
        return str(args.get("text") or "")
    if tool_name == "create_session":
        return str(args.get("command") or "")
    return ""


# Shell metacharacters that chain commands. If either the prefix entry OR
# the live primary text contains any of these, allowlist matching is voided —
# `prefix="git "` must NOT allowlist `"git status; rm -rf /"`.
_METACHAR_RE = re.compile(r"[;&|`$><\n\r]|\$\(|\|\|")


def is_allowlisted(tool_name: str, args: dict) -> bool:
    primary = _primary_text(tool_name, args).strip()
    if not primary or _METACHAR_RE.search(primary):
        return False
    for entry in _load_allowlist():
        if entry.get("tool") != tool_name:
            continue
        prefix = (entry.get("prefix") or "").strip()
        if not prefix or _METACHAR_RE.search(prefix):
            # Reject metachar-containing entries at lookup time so a user
            # can't write `prefix: "ls; rm -rf"` and feel safe.
            continue
        # Token-bounded match: exact, or prefix followed by whitespace.
        if primary == prefix or primary.startswith(prefix + " "):
            return True
    return False


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


def _log_audit(record: dict, suffix: str = "") -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    tag = record.get("tool", "unknown")
    name = f"{ts}-{uuid.uuid4().hex[:6]}-{tag}"
    if suffix:
        name = f"{name}-{suffix}"
    path = AUDIT_DIR / f"{name}.json"
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
    return path


# ---------------------------------------------------------------------------
# Broadcaster registry — same shape as soul_review.
# ---------------------------------------------------------------------------


_broadcasters: list[Callable[[dict], Awaitable[None]]] = []
# req_id -> future. Single-event-loop assumption: futures are created with
# `loop.create_future()` from the asyncio loop that calls `gate()`, and
# `set_result` runs on that same loop via `resolve_decision`. If a future
# Roboot ever runs the gate from multiple loops (worker thread doing its
# own asyncio), revisit this — set_result across loops will raise.
_pending: dict[str, asyncio.Future] = {}


def register_broadcaster(fn: Callable[[dict], Awaitable[None]]) -> None:
    if fn not in _broadcasters:
        _broadcasters.append(fn)


def unregister_broadcaster(fn: Callable[[dict], Awaitable[None]]) -> None:
    try:
        _broadcasters.remove(fn)
    except ValueError:
        pass


def resolve_decision(req_id: str, approved: bool) -> bool:
    """Called by a WS handler on receipt of a `tool_approval_decision` frame.

    Returns True if a pending request was actually resolved. False means the
    request already timed out or never existed (stale click).
    """
    fut = _pending.pop(req_id, None)
    if fut is None or fut.done():
        return False
    fut.set_result(approved)
    return True


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _args_summary(tool_name: str, args: dict) -> str:
    primary = _primary_text(tool_name, args)
    if primary:
        return primary
    return json.dumps(args, ensure_ascii=False)


async def gate(
    tool_name: str,
    args: dict,
    *,
    origin: str = "unknown",
    timeout: float = DEFAULT_TIMEOUT_S,
) -> Decision:
    """The single approval entry point.

    Logic (precedence matters — danger always wins over allowlist):
      1. mode=OFF → AUTO.
      2. unknown tool → AUTO (only the v1 gated set is policed).
      3. tool=shell + no danger pattern → AUTO (the common case, fast path).
      4. always-confirm tool (non-shell) + allowlist hit → AUTO.
         shell + danger → allowlist CANNOT override; falls through to modal.
      5. summary > MAX_ARGS_BYTES → REJECTED.
      6. mode=LOG (or no broadcasters in CONFIRM) → LOGGED + audit + allow.
      7. mode=CONFIRM → broadcast frame, await reply or timeout.
    """
    primary = _primary_text(tool_name, args)
    danger = detect_dangerous(primary) if tool_name == "shell" else None

    mode = get_mode()
    if mode == Mode.OFF:
        return Decision.AUTO
    if tool_name not in {"shell", "send_to_session", "create_session", "enroll_face"}:
        # Unknown tool — don't gate. Future tools opt in.
        return Decision.AUTO
    if tool_name == "shell" and danger is None:
        return Decision.AUTO
    # Allowlist applies ONLY when there is no danger. A dangerous shell
    # command must always go to modal — the allowlist is for suppressing
    # routine confirmations on always-confirm tools, not for waiving danger.
    if danger is None and is_allowlisted(tool_name, args):
        return Decision.AUTO

    summary = _args_summary(tool_name, args)
    record_base = {
        "tool": tool_name,
        "args_summary": summary,
        "danger_reason": danger,
        "origin": origin,
        "ts": time.time(),
    }

    if len(summary.encode("utf-8")) > MAX_ARGS_BYTES:
        logger.warning(
            "tool_guard: args summary > %d bytes (tool=%s), rejecting",
            MAX_ARGS_BYTES,
            tool_name,
        )
        _log_audit(record_base, suffix="REJECTED-OVERSIZE")
        return Decision.REJECTED

    if mode == Mode.LOG:
        _log_audit(record_base)
        return Decision.LOGGED

    # mode == CONFIRM
    if not _broadcasters:
        logger.warning("tool_guard: no broadcasters in CONFIRM, degrading to LOG")
        _log_audit(record_base, suffix="DEGRADED-LOG")
        return Decision.LOGGED

    req_id = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut = loop.create_future()
    _pending[req_id] = fut

    frame = {
        "type": "tool_approval",
        "req_id": req_id,
        "tool": tool_name,
        "args_summary": summary,
        "danger_reason": danger,
        "origin": origin,
        "issued_at": time.time(),
        "timeout_s": timeout,
    }
    for bc in list(_broadcasters):
        try:
            await bc(frame)
        except Exception as e:
            logger.warning("tool_guard broadcaster failed: %s", e)

    try:
        approved = await asyncio.wait_for(fut, timeout=timeout)
    except asyncio.TimeoutError:
        _log_audit(record_base, suffix="TIMEOUT")
        return Decision.REJECTED
    finally:
        # Always clean up — wait_for may also be cancelled externally
        # (task cancel, reload), in which case we'd otherwise leak the
        # entry. Single-event-loop assumption: see _pending docstring.
        _pending.pop(req_id, None)

    if approved:
        _log_audit(record_base, suffix="APPROVED")
        return Decision.APPROVED
    _log_audit(record_base, suffix="REJECTED")
    return Decision.REJECTED


# ---------------------------------------------------------------------------
# Arcana adapter
# ---------------------------------------------------------------------------


# contextvars for origin tracking — set at message-receive boundary in
# server.py / adapters/telegram_bot.py / relay_client.py.
#
# WARN: until D2 wires the contextvar set/reset, every audit record will
# carry origin="local" — including Telegram-driven calls. This is exactly
# the case where origin matters most for forensics. Do not flip
# ROBOOT_TOOL_APPROVAL=confirm in a multi-surface deployment until D2 ships.
import contextvars  # noqa: E402  (placed near use site for locality)

current_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tool_guard_origin", default="local"
)


async def confirmation_callback(tool_call: Any, spec: Any) -> bool:
    """Adapter for `arcana.tool_gateway.gateway.ToolGateway.confirmation_callback`.

    Arcana calls this when a tool with `requires_confirmation=True` (or
    `side_effect=WRITE`) is about to run. Return True to allow, False to
    reject (Arcana surfaces a CONFIRMATION_REJECTED ToolError to the agent).

    Fail closed: any unexpected exception inside the gate returns False.
    A crashing security gate that lets traffic through is the textbook
    mistake; better to surface CONFIRMATION_REJECTED to the agent than
    to silently allow.
    """
    try:
        name = getattr(tool_call, "name", "?")
        raw_args = getattr(tool_call, "arguments", {}) or {}
        # Some Arcana paths surface arguments as a JSON string (streamed
        # tool_call before parse). Defensively coerce to dict.
        if isinstance(raw_args, str):
            try:
                raw_args = json.loads(raw_args)
            except json.JSONDecodeError:
                raw_args = {}
        if not isinstance(raw_args, dict):
            raw_args = {}
        origin = current_origin.get()
        decision = await gate(name, raw_args, origin=origin)
        return decision in {Decision.AUTO, Decision.LOGGED, Decision.APPROVED}
    except Exception:
        logger.exception("tool_guard: gate crashed; failing closed")
        return False
