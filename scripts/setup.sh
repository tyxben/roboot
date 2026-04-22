#!/usr/bin/env bash
#
# Roboot installer — one command to go from fresh clone to running bot.
# Idempotent: safe to re-run, skips steps that already succeeded.
#
# Usage:
#   ./scripts/setup.sh                   # default: telegram extras (voice I/O)
#   ./scripts/setup.sh --with=core       # minimal: just web console + relay
#   ./scripts/setup.sh --with=all        # + vision + macOS CLI voice + desktop
#   ./scripts/setup.sh --no-prewarm      # skip the ~3 GB model download
#
set -euo pipefail

# ----- args --------------------------------------------------------------

EXTRAS="telegram"
DO_PREWARM=1
while [ $# -gt 0 ]; do
    case "$1" in
        --with=*)      EXTRAS="${1#--with=}"   ;;
        --no-prewarm)  DO_PREWARM=0            ;;
        -h|--help)
            sed -n 's/^# \{0,1\}//p' "$0" | sed -n '/^Roboot installer/,/^$/p'
            exit 0
            ;;
        *)
            echo "unknown arg: $1" >&2
            exit 2
            ;;
    esac
    shift
done

# ----- helpers -----------------------------------------------------------

msg()  { printf '\033[1;34m[setup]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# ----- 1. platform + python sanity --------------------------------------

msg "checking environment…"

OS="$(uname -s)"
[ "$OS" = "Darwin" ] || warn "detected $OS — Roboot is macOS-only in practice (iTerm2 integration, mlx-whisper, camera). Continuing but expect breakage."

command -v python3 >/dev/null 2>&1 || die "python3 not found. Install Python 3.11+ first."

PY_VER="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
PY_OK="$(python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' && echo yes || echo no)"
[ "$PY_OK" = "yes" ] || die "Python 3.11+ required, found $PY_VER"
msg "  python $PY_VER ✓"

# Warn loudly when running inside the anaconda base env — mlx-whisper
# pulls numpy 2 and collides with conda's pinned numpy 1 stack.
if [ "${CONDA_DEFAULT_ENV:-}" = "base" ]; then
    warn "you're in anaconda 'base'. mlx-whisper + numpy 2 will likely conflict"
    warn "with conda's numpy 1 pins. Recommended: \`conda create -n roboot python=3.11\`"
    warn "then re-run this script from the new env. (Continuing anyway.)"
fi

# ----- 2. install deps (prefer uv — 10x faster resolver, better conflict
#         handling than pip on numpy-2-vs-numba-style collisions) --------

msg "installing python deps (extras: $EXTRAS)…"

case "$EXTRAS" in
    core)       EXTRAS_SPEC="" ;;
    telegram|voice|vision|desktop|all)
                EXTRAS_SPEC="[$EXTRAS]" ;;
    *)          die "unknown --with=$EXTRAS (use: core|telegram|voice|vision|desktop|all)" ;;
esac

if command -v uv >/dev/null 2>&1; then
    msg "  using uv $(uv --version 2>&1 | awk '{print $2}')"
    # `uv pip install -e` respects whatever interpreter is currently active
    # (venv / conda env / system python3) via the ambient VIRTUAL_ENV.
    if [ -f uv.lock ] && [ "$EXTRAS" = "all" ]; then
        uv sync --all-extras
    elif [ -f uv.lock ] && [ "$EXTRAS" != "core" ]; then
        uv sync --extra "$EXTRAS"
    elif [ -f uv.lock ]; then
        uv sync
    else
        uv pip install -e ".$EXTRAS_SPEC"
    fi
else
    msg "  uv not found — falling back to pip (consider installing uv for 10x speed)"
    msg "    curl -LsSf https://astral.sh/uv/install.sh | sh"
    python3 -m pip install --quiet --upgrade pip
    # shellcheck disable=SC2086
    python3 -m pip install --quiet -e ".$EXTRAS_SPEC"
fi
msg "  deps installed ✓"

# ----- 3. system deps (ffmpeg for Telegram voice) -----------------------

needs_ffmpeg=0
case "$EXTRAS" in telegram|all) needs_ffmpeg=1 ;; esac

if [ "$needs_ffmpeg" = 1 ]; then
    if command -v ffmpeg >/dev/null 2>&1; then
        msg "  ffmpeg $(ffmpeg -version 2>&1 | head -1 | awk '{print $3}') ✓"
    elif command -v brew >/dev/null 2>&1; then
        msg "installing ffmpeg via Homebrew…"
        brew install ffmpeg
    else
        warn "ffmpeg not found and Homebrew not installed."
        warn "Telegram voice replies need ffmpeg. Install it manually:"
        warn "  https://ffmpeg.org/download.html"
    fi
fi

# ----- 4. config.yaml ---------------------------------------------------

if [ ! -f config.yaml ]; then
    cp config.example.yaml config.yaml
    msg "created config.yaml (edit this and add your LLM API key)"
else
    msg "  config.yaml exists ✓ (not overwritten)"
fi

# ----- 5. prewarm ASR model --------------------------------------------

needs_prewarm=0
case "$EXTRAS" in telegram|all) needs_prewarm=1 ;; esac

if [ "$needs_prewarm" = 1 ] && [ "$DO_PREWARM" = 1 ]; then
    msg "prewarming Whisper ASR model (~3 GB, one-time)…"
    msg "  first run takes a few minutes; re-runs verify the cache and are instant."
    python3 -m adapters.stt prewarm || warn "prewarm failed — not fatal, first voice message will retry."
fi

# ----- 6. next-step hints ----------------------------------------------

cat <<'EOF'

[setup] done.

Next steps:
  1. Edit config.yaml — fill in providers.deepseek (or another LLM key)
     and, if you want Telegram, telegram.bot_token + telegram.allowed_users.
  2. Enable iTerm2's Python API:
        iTerm2 → Settings → General → Magic → Enable Python API
  3. Start the web console:
        python server.py        # http://localhost:8765
  4. (Optional) Start the Telegram bot in another shell:
        python -m adapters.telegram_bot

See README.md for entry-point details. See docs/USAGE.md for a walkthrough.
EOF
