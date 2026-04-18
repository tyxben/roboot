"""Shared pytest fixtures.

The repo root is added to `sys.path` so `import chat_store`, `import
chat_handler`, `import relay_client` work when pytest is invoked from
anywhere under the repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
