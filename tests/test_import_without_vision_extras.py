"""Regression: the daemon entry points must import without [vision] extras.

The CI Linux runners install `.[telegram]` but NOT `.[vision]` — numpy, cv2,
face_recognition and dlib are absent (dlib is slow/brittle to compile on
Linux). Historically the import chain

    server.py / run.py / adapters.telegram_bot
        → tools.vision
            → tools.face_db
                → import numpy        # module top  ← crash

made `import server` (and pytest collection of anything importing it) fail
with ModuleNotFoundError. The telegram path was patched with a try/except in
f822eca, but server.py / run.py kept the hard import — a latent landmine that
only stayed green because no test imported those entry points.

The real fix decouples at the producer side: tools.face_db imports numpy
lazily inside recognize(). This test pins it by simulating the CI environment
— numpy/cv2/face_recognition/dlib blocked via a meta_path finder in a fresh
subprocess — and asserting the entry points still import.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def test_entry_points_import_without_vision_extras():
    code = textwrap.dedent(
        """
        import sys

        BLOCKED = {"numpy", "cv2", "face_recognition", "dlib"}

        class _Blocker:
            # A meta_path finder that pretends the optional vision deps are
            # not installed, exactly like a Linux CI runner without [vision].
            def find_spec(self, name, path=None, target=None):
                if name.split(".")[0] in BLOCKED:
                    raise ModuleNotFoundError(f"blocked optional dep: {name}")
                return None

        # Drop anything already cached so the block actually bites.
        for mod in list(sys.modules):
            if mod.split(".")[0] in BLOCKED:
                del sys.modules[mod]
        sys.meta_path.insert(0, _Blocker())

        # Sanity: the block is live.
        try:
            import numpy  # noqa: F401
        except ModuleNotFoundError:
            pass
        else:
            raise AssertionError("blocker did not block numpy")

        # The actual assertions: these must import with vision deps absent.
        import tools.face_db   # noqa: F401
        import tools.vision    # noqa: F401
        import run             # noqa: F401
        import server          # noqa: F401

        print("IMPORT_OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"entry points failed to import without [vision] extras:\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "IMPORT_OK" in result.stdout, result.stdout
