"""``apps/api`` entrypoint: ``uvicorn apps.api.main:app``.

Kept as a thin shim so the repository layout matches the documented structure
while the implementation stays in the importable ``recallops`` package.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:  # allow `uvicorn apps.api.main:app` from the repo root
    sys.path.insert(0, str(REPO_ROOT))

from recallops.main import app, create_app  # noqa: E402

__all__ = ["app", "create_app"]
