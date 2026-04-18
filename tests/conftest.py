"""Shared pytest fixtures.

The repo root is added to `sys.path` so `import chat_store`, `import
chat_handler`, `import relay_client` work when pytest is invoked from
anywhere under the repo.

An autouse fixture also redirects the daemon identity key into a session-
scoped tmp dir so tests never touch the user's real `.identity/`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


@pytest.fixture(autouse=True)
def _isolate_identity(tmp_path_factory, monkeypatch):
    """Prevent tests from creating / reading the user's .identity/ key."""
    import identity

    scratch = tmp_path_factory.mktemp("identity")
    monkeypatch.setattr(identity, "IDENTITY_DIR", scratch)
    monkeypatch.setattr(identity, "KEY_PATH", scratch / "daemon.ed25519.key")
