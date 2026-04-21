"""Cheap guardrails on `scripts/setup.sh`.

The script is the first thing a new user runs — silent breakage here
means they get a cryptic error before even seeing the web console.
These tests don't actually execute the install (they'd pull ~3 GB of
model weights); they just check the script parses, stays executable,
and rejects unknown flags correctly."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup.sh"


def test_script_exists_and_is_executable():
    assert SCRIPT.exists(), f"{SCRIPT} missing"
    mode = SCRIPT.stat().st_mode
    assert mode & stat.S_IXUSR, "setup.sh must be chmod +x so users can run it"


def test_script_has_valid_bash_syntax():
    """`bash -n` parses the script without executing — catches typos,
    unclosed quotes, unterminated heredocs, etc."""
    result = subprocess.run(
        ["bash", "-n", str(SCRIPT)], capture_output=True, text=True
    )
    assert result.returncode == 0, f"bash -n failed: {result.stderr}"


def test_script_help_flag_exits_zero():
    result = subprocess.run(
        [str(SCRIPT), "--help"], capture_output=True, text=True, timeout=10
    )
    assert result.returncode == 0
    assert "Roboot installer" in result.stdout


def test_script_rejects_unknown_flag():
    result = subprocess.run(
        [str(SCRIPT), "--not-a-real-flag"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "unknown" in result.stderr.lower()


@pytest.mark.parametrize(
    "extras",
    ["core", "telegram", "voice", "vision", "desktop", "all"],
)
def test_script_accepts_each_advertised_extras_choice(extras, tmp_path, monkeypatch):
    """Every --with=<x> value mentioned in the help text must parse — catches
    the classic "docs say one thing, code does another" drift.

    We short-circuit before the real work by setting PIP to /bin/true via
    PATH manipulation; the script's arg-parse happens first and exits before
    anything expensive runs. If arg-parse dies the test fails."""
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    # Stub out tools the script calls after arg parsing so we don't actually
    # install / download anything in CI.
    for name in ("python3", "pip", "brew", "ffmpeg"):
        stub = fake_bin / name
        stub.write_text("#!/bin/sh\nexit 0\n")
        stub.chmod(0o755)
    # python3 -V is called for the version check; stub it more carefully.
    (fake_bin / "python3").write_text(
        '#!/bin/sh\ncase "$*" in *sys.version_info*) echo "3.11" ;; *) exit 0 ;; esac\n'
    )
    (fake_bin / "python3").chmod(0o755)

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"

    # Skip prewarm to avoid touching network/hf cache even with stubs.
    result = subprocess.run(
        [str(SCRIPT), f"--with={extras}", "--no-prewarm"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        cwd=str(SCRIPT.parent.parent),
    )
    # Script may succeed or fail on pip install stub (stub returns 0), but
    # what we're asserting is that arg parsing accepts the value — a bad
    # --with= would exit 2 with "unknown --with=<x>".
    assert "unknown --with=" not in result.stderr, result.stderr
