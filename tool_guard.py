"""Approval gate for agent tool calls.

Hooks into Arcana's `ToolGateway.confirmation_callback` so that any tool
flagged `requires_confirmation=True` (or `side_effect=WRITE`) routes through
this module before execution. The name-keyed set is `shell` (danger-pattern
matched on the command), the iTerm write tools `send_to_session` /
`create_session`, `enroll_face`, and the filesystem writers `write_file` /
`edit_file` (always-confirm in CONFIRM mode, keyed on the target path,
allowlistable). Roboot's OTHER native writes (reminders/todos/notes/voice) are
registered via `set_native_tools()` and stay AUTO — they were never in the
gated set and aren't now. But any UNKNOWN / external tool Arcana flags WRITE /
requires_confirmation — e.g. an MCP write tool such as `gmail.send_email`,
which is NOT a native tool — is gated by default (side-effect-first, not
name-allowlisted): without this an undeclared write tool reaching
`confirmation_callback` would short-circuit to AUTO and bypass approval
entirely (the tool-name-allowlist hole; the MCP tool-poisoning /
unknown-WRITE-bypass class, CVE-2025-54136). A tool that explicitly declares
`requires_confirmation=True` is always gated. Unknown READ/none tools that are
not flagged short-circuit to AUTO.

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

# Tools gated by NAME. `shell` is DANGER-MATCHED — gated only when its command
# trips a danger pattern (safe commands fast-path to AUTO). The rest are
# ALWAYS-CONFIRM — every call is gated in CONFIRM mode (allowlistable by their
# primary text). These names are RESERVED for Roboot-native tools: the
# fast-path trusts `_primary_text` to know each tool's primary arg. An external
# tool can't collide (Arcana namespaces MCP tools as "server.tool"), but a
# future native tool must not reuse these names with a different arg shape.
_DANGER_MATCHED_TOOLS = {"shell"}
_ALWAYS_CONFIRM_TOOLS = {
    "send_to_session",
    "create_claude_session",
    "enroll_face",
    "write_file",
    "edit_file",
}

# Names of Roboot's OWN (native, vetted) tools — populated at startup via
# `set_native_tools()`. The side-effect-first path in `gate()` gates UNKNOWN /
# external WRITE tools (e.g. MCP) by default, but native low-risk writes
# (reminders/todos/notes/voice) must stay AUTO as they always were. Empty
# (never registered) fails safe: every write gates. Native tools still gate
# when name-keyed above, or when they explicitly set requires_confirmation.
_native_tools: set[str] = set()

# Origins of UNATTENDED, self-clocked agent turns (e.g. the daily briefing).
# There is no human present to approve a gate, and these turns often run over
# untrusted content — so they are forced READ-ONLY: gate() REJECTS any
# name-keyed / requires_confirmation / write-side-effect tool from these
# origins, regardless of ROBOOT_TOOL_APPROVAL mode. An injected briefing can
# read but never write / shell / exfil. Future autonomous features add their
# origin here.
_AUTONOMOUS_ORIGINS: set[str] = {"briefing"}


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
    # Generalized pipe-to-shell / interpreter — NOT anchored on a download
    # command, so a payload decoded LOCALLY and piped into a shell still
    # trips the gate: `echo … | base64 -d | sh`, `xxd -r -p x | sh`,
    # `openssl enc -d -in x | bash`, `cat payload | sh`. The download-anchored
    # rules above stay first so curl/wget cases keep their descriptive reason.
    # Optional wrapper (sudo|nohup|env|exec) and optional /abs/path/ before
    # the shell name; `\b` after the name prevents matching `bash_completion`.
    (
        r"\|\s*(sudo\s+|nohup\s+|env\s+|exec\s+)?(\S*/)?"
        r"(ba|z|k|tc|c|da|fi)?sh\b",
        "pipe payload to shell",
    ),
    (
        r"\|\s*(sudo\s+|nohup\s+|env\s+|exec\s+)?(\S*/)?"
        r"(python[23]?|perl|ruby|node|php|lua|deno)\b",
        "pipe payload to interpreter",
    ),
    (r"<\(\s*(curl|wget|fetch|http)\b", "process-substitution remote payload"),
    # Shell/interpreter consuming a process substitution of ANY command
    # (`bash <(base64 -d x)`), not just a download.
    (r"\b(ba|z|k|tc|c|da|fi)?sh\s+<\(", "shell reads process substitution"),
    (
        r"\b(python[23]?|perl|ruby|node|php)\s+<\(",
        "interpreter reads process substitution",
    ),
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
    if tool_name == "create_claude_session":
        return str(args.get("initial_prompt") or args.get("directory") or "")
    if tool_name in ("write_file", "edit_file"):
        return str(args.get("path") or "")
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


def _log_audit(record: dict, suffix: str = "") -> Path | None:
    """Write an audit record. Best-effort: a write failure (disk full,
    read-only fs) must NEVER propagate. `gate()` calls this on the APPROVED
    branch *before* returning the user's decision, so an exception here would
    turn a command the user just approved into a silent rejection. Audit I/O
    is observability, not a gate input — log and continue.
    """
    try:
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        tag = record.get("tool", "unknown")
        name = f"{ts}-{uuid.uuid4().hex[:6]}-{tag}"
        if suffix:
            name = f"{name}-{suffix}"
        path = AUDIT_DIR / f"{name}.json"
        path.write_text(json.dumps(record, ensure_ascii=False, indent=2))
        return path
    except Exception as e:
        logger.warning("tool_guard: audit write failed (%s); continuing", e)
        return None


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


def set_native_tools(names: set[str]) -> None:
    """Register the names of Roboot's OWN (native, vetted) tools.

    The side-effect-first path in `gate()` gates UNKNOWN/external WRITE tools
    (e.g. MCP) by default, but must NOT start gating Roboot's own low-risk
    writes (reminders/todos/notes/voice) that were AUTO before. Native tools
    listed here are exempt from the side-effect-first path; they still gate if
    name-keyed (shell / iTerm / file writes) or if they explicitly declare
    `requires_confirmation`.

    Call ONCE at startup, AFTER registering native tools but BEFORE any
    `connect_mcp()`, so MCP tools are NOT captured as native. Empty set
    (never registered) fails safe: every write gates.
    """
    global _native_tools
    _native_tools = set(names)


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
    side_effect: str | None = None,
    requires_confirmation: bool = False,
) -> Decision:
    """The single approval entry point.

    `side_effect` ("write"/"read"/"none") and `requires_confirmation` come from
    the tool's Arcana spec via `confirmation_callback`. Direct callers (tests,
    in-process) may omit them — name-keyed tools gate the same way they always
    have; only UNKNOWN tools change behaviour, and only when flagged WRITE.

    Logic (precedence matters — danger always wins over allowlist):
      0. autonomous origin (briefing etc.) + any keyed/confirm/write tool →
         REJECTED, regardless of mode (no human to approve; read-only sandbox).
      1. mode=OFF → AUTO.
      2. Gating eligibility (precedence: native name-keyed policy → explicit
         confirmation → side-effect-first for unknown/external writes → AUTO):
           - name in _DANGER_MATCHED_TOOLS (shell): gate ONLY if the command
             trips a danger pattern; safe commands fast-path to AUTO.
           - name in _ALWAYS_CONFIRM_TOOLS: every call gated.
           - requires_confirmation set: gated (explicit author request).
           - side_effect=write AND not a native tool (e.g. an MCP write):
             gated by default — closes the unknown/external-write bypass.
           - else (native low-risk write, unknown READ/none, unflagged): AUTO.
      3. no danger + allowlist hit → AUTO.
         A dangerous shell command CANNOT be allowlisted; falls through to modal.
      4. summary > MAX_ARGS_BYTES → REJECTED.
      5. mode=LOG (or no broadcasters in CONFIRM) → LOGGED + audit + allow.
      6. mode=CONFIRM → broadcast frame, await reply or timeout.
    """
    primary = _primary_text(tool_name, args)
    danger = detect_dangerous(primary) if tool_name in _DANGER_MATCHED_TOOLS else None
    se = (side_effect or "").strip().lower()

    # Autonomous origins (unattended, self-clocked turns like the daily
    # briefing) have NO human to approve a gate and often run over untrusted
    # content — so they are READ-ONLY: any name-keyed tool (shell / iTerm /
    # file-writes / enroll_face), any requires_confirmation tool, or any write
    # side-effect is REJECTED outright, regardless of mode. An injected briefing
    # can read but never write / shell / exfil. Checked BEFORE the OFF
    # short-circuit so `off` installs are protected here too.
    if origin in _AUTONOMOUS_ORIGINS and (
        tool_name in _DANGER_MATCHED_TOOLS
        or tool_name in _ALWAYS_CONFIRM_TOOLS
        or requires_confirmation
        or se == "write"
    ):
        _log_audit(
            {
                "tool": tool_name,
                "args_summary": _args_summary(tool_name, args),
                "danger_reason": danger or f"autonomous origin blocked ({se or 'keyed'})",
                "side_effect": se or None,
                "origin": origin,
                "ts": time.time(),
            },
            suffix="REJECTED-AUTONOMOUS",
        )
        return Decision.REJECTED

    mode = get_mode()
    if mode == Mode.OFF:
        return Decision.AUTO

    # Gating eligibility — native name-keyed policy first, then side-effect-first.
    reason = danger
    if tool_name in _DANGER_MATCHED_TOOLS:
        # shell: gate ONLY dangerous commands. Safe ones fast-path to AUTO even
        # though shell is side_effect=write+requires_confirmation — we don't
        # modal-spam every `ls`.
        if danger is None:
            return Decision.AUTO
    elif tool_name in _ALWAYS_CONFIRM_TOOLS:
        pass  # native high-risk write — every call gated (allowlistable below)
    elif requires_confirmation:
        # Explicitly flagged by the tool author — gate whether native or not.
        reason = reason or "requires_confirmation"
    elif se == "write" and tool_name not in _native_tools:
        # UNKNOWN / external WRITE — e.g. an MCP write tool (gmail.send_email).
        # Arcana only reaches this callback for WRITE/requires_confirmation
        # tools, so an unrecognised one is an external action worth approving.
        # Native low-risk writes are in _native_tools and fall through to AUTO.
        reason = f"unknown WRITE tool (side_effect={se or 'inferred'})"
    else:
        # Native low-risk write, or unknown READ/none, or unflagged — don't gate.
        return Decision.AUTO

    # Allowlist applies ONLY when there is no danger. A dangerous shell
    # command must always go to modal — the allowlist is for suppressing
    # routine confirmations on always-confirm tools, not for waiving danger.
    # Unknown WRITE tools have no `_primary_text` extractor, so `is_allowlisted`
    # returns False for them — they cannot be prefix-allowlisted today.
    if danger is None and is_allowlisted(tool_name, args):
        return Decision.AUTO

    summary = _args_summary(tool_name, args)
    record_base = {
        "tool": tool_name,
        "args_summary": summary,
        "danger_reason": reason,
        "side_effect": se or None,
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
        "danger_reason": reason,
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


# contextvars for origin tracking — set/reset at the message-receive
# boundary in server.py ("local"), adapters/telegram_bot.py ("telegram"),
# and relay_client.py ("relay"), so each audit record and the broadcast
# frame carry the surface the call actually came from. The default "local"
# covers direct/in-process callers that never set it.
import contextvars  # noqa: E402  (placed near use site for locality)

current_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "tool_guard_origin", default="local"
)


def _coerce_side_effect(side_effect: Any) -> str | None:
    """Normalize Arcana's `SideEffect` enum (or a raw string) to its lowercase
    value ('write'/'read'/'none'), or None if absent.

    tool_guard deliberately carries no `arcana` import, so we duck-type the
    enum's `.value` rather than compare against `SideEffect.WRITE`.
    """
    if side_effect is None:
        return None
    val = getattr(side_effect, "value", side_effect)
    return str(val).strip().lower() or None


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
        side_effect = _coerce_side_effect(getattr(spec, "side_effect", None))
        requires_confirmation = bool(getattr(spec, "requires_confirmation", False))
        # Arcana only invokes this callback for WRITE / requires_confirmation
        # tools. If the spec is malformed and we can read NEITHER signal, fail
        # closed — treat it as a write so an unknown tool gates, not slips by.
        if side_effect is None and not requires_confirmation:
            side_effect = "write"
        decision = await gate(
            name,
            raw_args,
            origin=origin,
            side_effect=side_effect,
            requires_confirmation=requires_confirmation,
        )
        return decision in {Decision.AUTO, Decision.LOGGED, Decision.APPROVED}
    except Exception:
        logger.exception("tool_guard: gate crashed; failing closed")
        return False
