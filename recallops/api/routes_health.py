"""Health routes, including network/connectivity diagnostics for providers.

Three levels are exposed, matching what an operator (or a service manager) needs:

* ``/health/live``   - the process is running. Never touches the database.
* ``/health/ready``  - the process can serve traffic: readable, writable,
  schema-complete database. Returns 503 when it cannot.
* ``/health``        - the full component report (additive ``runtime`` block).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query, Response, status
from sqlalchemy import text

from recallops.api.deps import get_container
from recallops.memory.models import MEMORY_MODE_LABELS
from recallops.persistence import models as orm
from recallops.persistence.db import readiness_probe
from recallops.services import runtime
from recallops.services.resilience import HINTS, ErrorKind, diagnose_endpoint

router = APIRouter(tags=["health"])


@router.get("/health/live")
def health_live() -> dict[str, Any]:
    """Liveness: is the process up? Deliberately dependency-free.

    A liveness probe that consults Hindsight or the database will restart a
    perfectly healthy API during a dependency outage, turning a degradation
    into an outage.
    """
    return {
        "status": "alive",
        "runtime": runtime.snapshot(),
        "checked_at": time.time(),
    }


@router.get("/health/ready")
def health_ready(response: Response) -> dict[str, Any]:
    """Readiness: can this process actually serve requests right now?

    Returns 503 when the database is missing, read-only, or half-created, so a
    service manager stops routing traffic instead of serving errors.
    """
    probe = readiness_probe()
    ready = bool(probe.get("ready"))
    runtime.mark_ready(ready, "ready" if ready else f"database not ready: {probe.get('error') or probe.get('missing_tables')}")
    runtime.mark_checked()
    if not ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {
        "status": "ready" if ready else "not_ready",
        "checks": {
            "database_readable": probe.get("writable"),
            "database_writable": probe.get("writable"),
            "schema_complete": probe.get("schema_ok"),
            "missing_tables": probe.get("missing_tables"),
            "journal_mode": probe.get("journal_mode"),
            "db_path": probe.get("db_path"),
            "latency_ms": probe.get("latency_ms"),
            "error": probe.get("error"),
        },
        "runtime": runtime.snapshot(),
        "checked_at": time.time(),
    }


@router.get("/health")
def health() -> dict[str, Any]:
    container = get_container()
    settings = container.settings
    memory_health = container.memory.health()
    llm_health = _llm_health(container)
    db_state, db_detail = _database_health()
    healthy = db_state == "connected"
    return {
        "status": "ok" if healthy else "degraded",
        "app": settings.app_name,
        "version": settings.app_version,
        "environment": settings.environment,
        "demo_mode": settings.demo_mode,
        "runtime": runtime.snapshot(),
        "components": {
            "database": {"state": db_state, "detail": db_detail},
            "memory": {"state": memory_health.state, "mode": memory_health.mode.value, "detail": memory_health.detail},
            "llm": {"state": llm_health.get("state"), "provider": llm_health.get("provider"), "detail": llm_health.get("detail")},
            "simulator": {"state": "connected", "detail": f"{len(container.orchestrator.scenarios)} seeded scenarios"},
        },
        "config": settings.public_snapshot(),
        "checked_at": time.time(),
    }


@router.get("/health/memory")
def health_memory() -> dict[str, Any]:
    container = get_container()
    health_payload = container.memory.health().to_dict()
    health_payload["mode_labels"] = MEMORY_MODE_LABELS
    return health_payload


@router.get("/health/hindsight")
def health_hindsight(probe: bool = Query(default=False, description="Run a live TCP/TLS/readiness probe")) -> dict[str, Any]:
    container = get_container()
    settings = container.settings
    payload: dict[str, Any] = {
        "configured": bool(settings.hindsight_endpoint),
        "endpoint": settings.hindsight_endpoint,
        "bank_id": settings.hindsight_bank_id,
        "api_key_configured": bool(settings.hindsight_api_key),
        "enabled_mode": settings.hindsight_enabled,
        "timeout_s": settings.hindsight_timeout,
    }
    if not settings.hindsight_endpoint:
        payload.update(
            {
                "state": "unavailable",
                "detail": "HINDSIGHT_BASE_URL is not set. Set it (docker compose up -d hindsight) to use real Hindsight memory.",
                "fallback": "the system is using the documented local memory mirror",
                "hints": [
                    "Hindsight runs locally on port 8888 (docker compose up -d hindsight).",
                    "Hindsight Cloud uses a different host and requires an API key.",
                ],
            }
        )
        return payload
    payload["health"] = container.memory.health().to_dict()
    if probe:
        payload["network"] = asyncio_safe_diagnose(settings.hindsight_endpoint or "", settings.network_probe_timeout)
    return payload


@router.get("/health/llm")
def health_llm() -> dict[str, Any]:
    container = get_container()
    return _llm_health(container)


@router.get("/health/database")
def health_database() -> dict[str, Any]:
    container = get_container()
    state, detail = _database_health()
    probe = readiness_probe()
    session = container.orchestrator.session()
    try:
        counts = {
            "incidents": session.query(orm.Incident).count(),
            "evidence": session.query(orm.EvidenceEvent).count(),
            "hypotheses": session.query(orm.Hypothesis).count(),
            "actions": session.query(orm.ActionAttempt).count(),
            "postmortems": session.query(orm.Postmortem).count(),
            "memory_records": session.query(orm.MemoryRecord).count(),
        }
    finally:
        session.close()
    return {
        "state": state,
        "detail": detail,
        "rows": counts,
        "url_scheme": container.settings.sqlalchemy_url.split(":", 1)[0],
        "durability": {
            "db_path": probe.get("db_path"),
            "journal_mode": probe.get("journal_mode"),
            "writable": probe.get("writable"),
            "schema_ok": probe.get("schema_ok"),
            "missing_tables": probe.get("missing_tables"),
        },
    }


@router.get("/health/network")
def health_network(url: str | None = Query(default=None, description="Endpoint to diagnose; defaults to Hindsight")) -> dict[str, Any]:
    container = get_container()
    target = url or container.settings.hindsight_endpoint
    if not target:
        return {
            "ok": False,
            "verdict": "No endpoint configured (HINDSIGHT_BASE_URL is empty).",
            "hints": list(HINTS[ErrorKind.DNS]),
        }
    return asyncio_safe_diagnose(target, container.settings.network_probe_timeout)


@router.get("/health/errors")
def health_error_reference() -> dict[str, Any]:
    """The error taxonomy the UI renders, with the diagnosis shown to engineers."""
    return {
        "kinds": {
            kind.value: {
                "retryable": kind.value in {"connect_timeout", "dns_failure", "connection_refused", "tls_failure", "rate_limited", "server_error"},
                "hints": list(hints),
            }
            for kind, hints in HINTS.items()
        }
    }


def _llm_health(container: Any) -> dict[str, Any]:
    import asyncio

    try:
        health = asyncio.run(container.llm.health())
        payload = health.to_dict()
    except Exception as exc:  # noqa: BLE001
        from recallops.services.resilience import HealthState, ProviderHealth, classify_exception

        kind = classify_exception(exc)
        payload = ProviderHealth(name="llm", state=HealthState.UNAVAILABLE, detail=f"{kind.value}: {exc}"[:200], kind=kind, hints=list(HINTS.get(kind, []))).to_dict()
    payload["describe"] = container.llm.describe()
    payload["mode_label"] = (
        "Deterministic rule engine (no external model call)"
        if container.llm.mode == "local_heuristic"
        else f"API model: {container.llm.model}"
    )
    return payload


def _database_health() -> tuple[str, str]:
    container = get_container()
    session = container.orchestrator.session()
    try:
        started = time.perf_counter()
        session.execute(text("SELECT 1"))
        latency = int((time.perf_counter() - started) * 1000)
        return "connected", f"SQLite responded in {latency}ms"
    except Exception as exc:  # noqa: BLE001
        return "unavailable", f"database error: {exc}"[:200]
    finally:
        session.close()


def asyncio_safe_diagnose(endpoint: str, timeout: float) -> dict[str, Any]:
    import asyncio

    async def run() -> dict[str, Any]:
        return await diagnose_endpoint(endpoint, probe_timeout=timeout)

    try:
        return asyncio.run(run())
    except Exception as exc:  # noqa: BLE001
        from recallops.services.resilience import classify_exception

        kind = classify_exception(exc)
        return {"ok": False, "endpoint": endpoint, "error_kind": kind.value, "verdict": f"diagnosis failed: {exc}"[:200], "hints": list(HINTS.get(kind, []))}


__all__ = ["router"]
