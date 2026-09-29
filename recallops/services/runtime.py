"""Process identity and readiness state for long-running (24/7) operation.

An SRE console needs to answer three questions that ``/health`` alone cannot:

* **Is this process alive?** (liveness - must never depend on a downstream
  provider, otherwise a Hindsight outage triggers a restart loop)
* **Can this process serve traffic?** (readiness - requires a readable *and
  writable* database with the expected schema)
* **Is the backend I am talking to the same one as a moment ago?** (instance
  identity - the SSE stream is in-process, so a restart silently truncates the
  live timeline and the UI must resync from the durable record)

Keeping this in one small module means the health routes stay declarative and
the frontend can read a single ``runtime`` block from any endpoint.
"""

from __future__ import annotations

import os
import socket
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any

_lock = threading.Lock()
_instance_id: str = ""
_started_at: float = time.time()
_started_at_iso: str = ""
_ready = False
_ready_detail: str = "starting"
_last_readiness_check: float = 0.0


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


def mark_started() -> str:
    """Assign this process an identity. Idempotent."""
    global _instance_id, _started_at, _started_at_iso
    with _lock:
        if not _instance_id:
            _instance_id = uuid.uuid4().hex[:12]
            _started_at = time.time()
            _started_at_iso = _iso(_started_at)
        return _instance_id


def mark_ready(ready: bool, detail: str = "") -> None:
    global _ready, _ready_detail
    with _lock:
        _ready = ready
        _ready_detail = detail or ("ready" if ready else "not ready")


def is_ready() -> bool:
    return _ready


def instance_id() -> str:
    return _instance_id or mark_started()


def uptime_s() -> float:
    return max(0.0, time.time() - _started_at)


def mark_checked() -> None:
    global _last_readiness_check
    with _lock:
        _last_readiness_check = time.time()


def snapshot() -> dict[str, Any]:
    """Small, JSON-safe block embedded in health payloads and SSE frames."""
    return {
        "instance_id": instance_id(),
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "started_at": _started_at_iso or _iso(_started_at),
        "uptime_s": round(uptime_s(), 3),
        "ready": _ready,
        "ready_detail": _ready_detail,
        "last_readiness_check": _iso(_last_readiness_check) if _last_readiness_check else None,
    }


__all__ = [
    "instance_id",
    "is_ready",
    "mark_checked",
    "mark_ready",
    "mark_started",
    "snapshot",
    "uptime_s",
]
