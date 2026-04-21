"""In-process code self-upgrade loop (opt-in).

Gate
----
This loop does nothing unless the env var ``ROBOOT_AUTO_UPGRADE=1`` is set
when ``server.py`` starts. That keeps dev checkouts (and CI) from ever
pulling code or re-execing behind the user's back. When enabled,
``server.py`` schedules ``run_upgrade_loop(app)`` in the startup hook.

What it does each tick (default: every hour)
--------------------------------------------
1. Record current HEAD sha.
2. ``git fetch origin main`` — any failure (offline, auth, etc.) is caught
   and logged; the loop keeps running.
3. ``git rev-list HEAD..origin/main --count`` — if 0, nothing to do.
4. ``git status --porcelain`` — if the working tree has local edits,
   pause (user is probably hacking).
5. Check ``server.get_in_flight_count()`` — if any chat turn is mid-stream,
   defer to the next tick so the user isn't interrupted mid-response.
6. ``git pull --ff-only origin main``.
7. Smoke test: ``python -m pytest tests/ -x --tb=no -q`` with a 60s budget.
   If tests fail or time out, ``git reset --hard <old_sha>`` and emit a
   loud log.
8. If tests pass, write ``.upgrade_pending`` (new_sha + iso timestamp),
   broadcast a notify frame to every connected console, and re-exec the
   current Python process via ``os.execv``.

Guarantees
----------
- Every subprocess call has a timeout.
- On any failure path the working tree ends up at a known-good sha
  (either before-pull or after-rollback).
- No shell=True, no user-controllable strings in subprocess args.
- Stdlib only (asyncio, subprocess, os, pathlib, time, datetime, logging).
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger("roboot.self_upgrade")

REPO_ROOT = Path(__file__).resolve().parent
SENTINEL_PATH = REPO_ROOT / ".upgrade_pending"

# Subprocess timeouts (seconds).
GIT_TIMEOUT = 30
FETCH_TIMEOUT = 60
PULL_TIMEOUT = 60
TEST_TIMEOUT = 60


class _SubprocessResult:
    __slots__ = ("returncode", "stdout", "stderr")

    def __init__(self, returncode: int, stdout: str, stderr: str) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


async def _run(
    *argv: str, cwd: Optional[Path] = None, timeout: float = GIT_TIMEOUT
) -> _SubprocessResult:
    """Run a subprocess with a hard timeout.

    Returns a _SubprocessResult; on timeout returncode is 124, on launch
    failure returncode is 127. Never raises for subprocess-level errors —
    callers decide what to do with non-zero.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except (FileNotFoundError, OSError) as e:
        log.error("[self_upgrade] failed to launch %s: %s", argv[0], e)
        return _SubprocessResult(127, "", str(e))

    try:
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        log.error(
            "[self_upgrade] command timed out after %ss: %s",
            timeout,
            " ".join(argv),
        )
        return _SubprocessResult(124, "", "timeout")

    return _SubprocessResult(
        proc.returncode if proc.returncode is not None else 1,
        stdout_b.decode("utf-8", errors="replace"),
        stderr_b.decode("utf-8", errors="replace"),
    )


async def _current_head() -> Optional[str]:
    r = await _run("git", "rev-parse", "HEAD", cwd=REPO_ROOT)
    if r.returncode != 0:
        log.error("[self_upgrade] rev-parse HEAD failed: %s", r.stderr.strip())
        return None
    sha = r.stdout.strip()
    return sha or None


async def _rev_parse(ref: str) -> Optional[str]:
    """Resolve an arbitrary ref (e.g. ``origin/main``) to a full SHA."""
    r = await _run("git", "rev-parse", ref, cwd=REPO_ROOT)
    if r.returncode != 0:
        log.warning(
            "[self_upgrade] rev-parse %s failed: %s", ref, r.stderr.strip()
        )
        return None
    sha = r.stdout.strip()
    return sha or None


async def _fetch_origin() -> bool:
    r = await _run(
        "git", "fetch", "origin", "main", cwd=REPO_ROOT, timeout=FETCH_TIMEOUT
    )
    if r.returncode != 0:
        log.warning(
            "[self_upgrade] git fetch failed (network?): %s", r.stderr.strip()
        )
        return False
    return True


async def _commits_behind() -> int:
    r = await _run(
        "git", "rev-list", "HEAD..origin/main", "--count", cwd=REPO_ROOT
    )
    if r.returncode != 0:
        log.error("[self_upgrade] rev-list failed: %s", r.stderr.strip())
        return 0
    try:
        return int(r.stdout.strip() or "0")
    except ValueError:
        return 0


async def _is_dirty() -> bool:
    r = await _run("git", "status", "--porcelain", cwd=REPO_ROOT)
    if r.returncode != 0:
        # If we can't tell, assume dirty — safer to skip than to clobber.
        log.error("[self_upgrade] git status failed: %s", r.stderr.strip())
        return True
    return bool(r.stdout.strip())


async def _pull_ff_only() -> bool:
    r = await _run(
        "git",
        "pull",
        "--ff-only",
        "origin",
        "main",
        cwd=REPO_ROOT,
        timeout=PULL_TIMEOUT,
    )
    if r.returncode != 0:
        log.error(
            "[self_upgrade] git pull --ff-only failed: %s", r.stderr.strip()
        )
        return False
    return True


async def _reset_hard(sha: str) -> bool:
    r = await _run("git", "reset", "--hard", sha, cwd=REPO_ROOT)
    if r.returncode != 0:
        log.error(
            "[self_upgrade] rollback reset --hard %s failed: %s",
            sha,
            r.stderr.strip(),
        )
        return False
    log.warning("[self_upgrade] rolled back to %s", sha[:12])
    return True


# Matches release-style tags: v0, v1.2, v0.3.0, v1.2.3-rc1, etc.
_RELEASE_TAG_RE = re.compile(r"^v[0-9][0-9A-Za-z.\-_+]*$")


async def _find_verified_tag_at(commit_sha: str) -> Optional[str]:
    """Return a release-style tag name whose signature verifies and which
    points at ``commit_sha``. Returns ``None`` if no such tag exists.

    Isolated as a module-level helper so tests can monkeypatch it without
    scripting both ``git tag`` and ``git verify-tag`` via ``_run``.
    """
    if not commit_sha:
        return None

    r = await _run("git", "tag", "--points-at", commit_sha, cwd=REPO_ROOT)
    if r.returncode != 0:
        log.warning(
            "[self_upgrade] git tag --points-at failed: %s", r.stderr.strip()
        )
        return None

    candidates = [
        line.strip()
        for line in r.stdout.splitlines()
        if line.strip() and _RELEASE_TAG_RE.match(line.strip())
    ]
    if not candidates:
        return None

    for tag in candidates:
        # git verify-tag exits 0 iff the tag has a GPG/SSH signature and it
        # validates against the caller's configured keyring / allowed_signers.
        vr = await _run("git", "verify-tag", tag, cwd=REPO_ROOT)
        if vr.returncode == 0:
            return tag
        log.info(
            "[self_upgrade] tag %s failed verification: %s",
            tag,
            (vr.stderr or vr.stdout).strip(),
        )

    return None


async def _run_smoke_tests() -> bool:
    r = await _run(
        sys.executable,
        "-m",
        "pytest",
        "tests/",
        "-x",
        "--tb=no",
        "-q",
        cwd=REPO_ROOT,
        timeout=TEST_TIMEOUT,
    )
    if r.returncode != 0:
        log.error(
            "[self_upgrade] smoke tests failed (rc=%s): %s",
            r.returncode,
            (r.stdout + r.stderr)[-500:],
        )
        return False
    return True


def _write_sentinel(new_sha: str) -> None:
    try:
        SENTINEL_PATH.write_text(
            f"{new_sha}\n{datetime.now(timezone.utc).isoformat()}\n",
            encoding="utf-8",
        )
    except OSError as e:
        log.warning("[self_upgrade] failed to write sentinel: %s", e)


async def _broadcast_upgrading(new_sha: str) -> None:
    """Best-effort broadcast of an upgrade notification to connected consoles.

    Reuses server's existing broadcast infrastructure. Any failure is
    swallowed — we don't want notification issues to block the upgrade.
    """
    short = new_sha[:7] if new_sha else "?"
    frame = {"type": "notify", "text": f"⬆️ 升级到 {short}，重启中..."}

    try:
        import server  # local import to avoid circular import on module load

        # Local web-console clients.
        clients = getattr(server, "_active_ws_clients", None)
        if clients:
            dead = []
            for client_ws in list(clients):
                try:
                    await client_ws.send_json(frame)
                except Exception:
                    dead.append(client_ws)
            for client_ws in dead:
                try:
                    clients.discard(client_ws)
                except Exception:
                    pass

        # Relay-connected clients, via server's helper + relay's own loop.
        relay = getattr(server, "_relay_client", None)
        relay_broadcast = getattr(server, "_relay_broadcast", None)
        if relay is not None and relay_broadcast is not None:
            loop = getattr(relay, "_loop", None)
            if loop is not None:
                try:
                    asyncio.run_coroutine_threadsafe(
                        relay_broadcast(relay, frame), loop
                    )
                except Exception:
                    pass
    except Exception as e:
        log.warning("[self_upgrade] broadcast failed: %s", e)


def restart_daemon(old_sha: Optional[str] = None) -> None:
    """Re-exec the current Python process with the same argv.

    If ``os.execv`` raises (it shouldn't on a healthy system), we attempt
    to roll back to ``old_sha`` synchronously and exit with a loud log.
    The caller has already written the sentinel and broadcast the notice.
    """
    log.warning(
        "[self_upgrade] re-exec: %s %s", sys.executable, " ".join(sys.argv)
    )
    try:
        os.execv(sys.executable, [sys.executable] + sys.argv)
    except Exception as e:
        log.error("[self_upgrade] os.execv failed: %s", e)
        # Best-effort synchronous rollback via plain subprocess (asyncio
        # loop state is unknown here). We do not re-raise; we exit.
        if old_sha:
            try:
                import subprocess

                subprocess.run(
                    ["git", "reset", "--hard", old_sha],
                    cwd=str(REPO_ROOT),
                    timeout=GIT_TIMEOUT,
                    check=False,
                )
                log.warning(
                    "[self_upgrade] rolled back to %s after exec failure",
                    old_sha[:12],
                )
            except Exception as inner:
                log.error(
                    "[self_upgrade] rollback after exec failure also failed: %s",
                    inner,
                )
        # Give the logger a moment to flush, then exit so supervisor may restart us.
        sys.exit(1)


async def _tick(app) -> None:
    """One upgrade check. All failures caught — never raise to the loop."""
    # 1. current sha
    old_sha = await _current_head()
    if not old_sha:
        return

    # 2. fetch
    if not await _fetch_origin():
        return

    # 3. any new commits?
    behind = await _commits_behind()
    if behind <= 0:
        return
    log.info("[self_upgrade] %d new commit(s) on origin/main", behind)

    # 4. working tree clean?
    if await _is_dirty():
        log.info("[self_upgrade] local edits present, upgrade paused")
        return

    # 5. any chat turn in flight?
    try:
        import server

        in_flight = int(server.get_in_flight_count())
    except Exception as e:
        log.warning("[self_upgrade] cannot read in_flight count: %s", e)
        in_flight = 0
    if in_flight > 0:
        log.info(
            "[self_upgrade] %d chat turn(s) in flight, deferring", in_flight
        )
        return

    # 5b. Optional gate: require a verified signed tag at origin/main.
    if os.environ.get("ROBOOT_UPGRADE_REQUIRE_SIGNED_TAG") == "1":
        remote_head = await _rev_parse("origin/main")
        if not remote_head:
            log.info(
                "[self_upgrade] skipping: cannot resolve origin/main"
            )
            return
        verified = await _find_verified_tag_at(remote_head)
        if not verified:
            log.info(
                "[upgrade] skipping: HEAD of origin/main not at a verified "
                "signed tag"
            )
            return
        log.info(
            "[self_upgrade] origin/main at verified tag %s, proceeding",
            verified,
        )

    # 6. pull
    if not await _pull_ff_only():
        # Pull with --ff-only can't corrupt the tree on failure; nothing to roll back.
        return

    new_sha = await _current_head()
    if not new_sha or new_sha == old_sha:
        log.warning("[self_upgrade] pull succeeded but HEAD unchanged; aborting")
        return
    log.warning(
        "[self_upgrade] pulled %s -> %s", old_sha[:12], new_sha[:12]
    )

    # 7. smoke test
    if not await _run_smoke_tests():
        log.error(
            "[self_upgrade] smoke tests failed, rolling back to %s",
            old_sha[:12],
        )
        await _reset_hard(old_sha)
        return

    # 8. commit -> exec
    _write_sentinel(new_sha)
    await _broadcast_upgrading(new_sha)
    # Give notify frames a moment to flush over the wire before we exec.
    try:
        await asyncio.sleep(0.5)
    except Exception:
        pass
    log.warning("[self_upgrade] upgrade OK, restarting")
    restart_daemon(old_sha=old_sha)


async def run_upgrade_loop(app, interval_s: int = 3600) -> None:
    """Periodic upgrade poll. Runs until cancelled.

    ``app`` is unused today but kept in the signature so callers can pass
    the FastAPI app; future ticks may want to read app state.
    """
    log.info(
        "[self_upgrade] loop started (interval=%ss, repo=%s)",
        interval_s,
        REPO_ROOT,
    )
    # Small initial delay so the server finishes coming up before we touch git.
    try:
        await asyncio.sleep(min(60, interval_s))
    except asyncio.CancelledError:
        return
    while True:
        try:
            await _tick(app)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("[self_upgrade] tick raised: %s", e)
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            return
