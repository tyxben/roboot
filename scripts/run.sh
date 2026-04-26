#!/usr/bin/env bash
# Roboot launcher. Boots every entry point through the project's
# uv-managed venv so the locked arcana-agent (>=0.8.2) is used, not
# whatever Python happens to be first on $PATH (looking at you, anaconda).
#
# Usage:
#   ./scripts/run.sh                      # web console (server.py)
#   ./scripts/run.sh server               # explicit
#   ./scripts/run.sh cli [--voice]        # run.py — keyboard or voice CLI
#   ./scripts/run.sh telegram             # adapters.telegram_bot
#   ./scripts/run.sh chainlit             # chainlit web UI
#   ./scripts/run.sh py <args...>         # raw `uv run python <args>` escape hatch
#   ./scripts/run.sh test [pytest-args]   # uv run python -m pytest tests/

set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if ! command -v uv >/dev/null 2>&1; then
  echo "uv is not on PATH. Install via scripts/setup.sh first." >&2
  exit 127
fi

# Cheap no-op when .venv is already there. Auto-creates on first run so
# a fresh `git clone && scripts/run.sh` works without a separate setup
# step. Telegram extra is the project default per scripts/setup.sh.
if [[ ! -d .venv ]]; then
  echo "Bootstrapping .venv (uv sync --extra telegram)..."
  uv sync --extra telegram
fi

mode="${1:-server}"
shift || true

case "$mode" in
  server)
    exec uv run python server.py "$@"
    ;;
  cli)
    exec uv run python run.py "$@"
    ;;
  telegram)
    exec uv run python -m adapters.telegram_bot "$@"
    ;;
  chainlit)
    exec uv run chainlit run chainlit_app.py -w "$@"
    ;;
  py)
    exec uv run python "$@"
    ;;
  test)
    # `dev` extra brings pytest + asyncio + respx. Sync once if missing.
    if ! .venv/bin/python -c "import pytest" 2>/dev/null; then
      uv sync --extra telegram --extra dev
    fi
    exec uv run python -m pytest tests/ "$@"
    ;;
  -h|--help|help)
    sed -n '2,13p' "$0"
    exit 0
    ;;
  *)
    echo "Unknown mode: $mode" >&2
    echo "Try: scripts/run.sh --help" >&2
    exit 2
    ;;
esac
