"""Liveness, readiness, process identity and SSE reconnection durability.

These tests exist because 24/7 operation fails in ways a happy-path suite never
sees: a service that stays "up" while its database is unwritable, and a live
timeline that silently loses events across a restart.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from recallops.domain.enums import TimelinePhase
from recallops.persistence import models as orm
from recallops.persistence.db import REQUIRED_TABLES, get_sessionmaker, readiness_probe
from recallops.services import runtime
from recallops.services.events import (
    _seq_from_last_event_id,
    event_stream,
    get_bus,
    replay_from_db,
    sse_event,
)


def _seed_events(incident_id: str, count: int, prefix: str = "e") -> list[str]:
    """Create one incident and ``count`` timeline events; return their titles."""
    session = get_sessionmaker()()
    try:
        session.add(orm.Incident(id=incident_id, scenario_id="INC-A1", service="payments", title=f"{incident_id} test"))
        session.flush()
        for i in range(count):
            record = None
            from recallops.services.events import record_event

            record = record_event(
                session,
                incident_id=incident_id,
                phase=TimelinePhase.EVIDENCE,
                title=f"{prefix}{i}",
            )
        session.commit()
        return [f"{prefix}{i}" for i in range(count)]
    finally:
        session.close()


async def _collect(topic: str, *, last_event_id: str | None = None, stop_after: int = 6) -> list[dict[str, Any]]:
    """Read frames from the SSE generator, then close it.

    The stream is infinite by design, so the test pulls a bounded number of
    frames and closes the generator instead of waiting for it to end. Keep-alives
    count towards the limit: after the ``ready`` event the next frame is always
    a heartbeat, which is what lets a test terminate deterministically.
    """
    frames: list[dict[str, Any]] = []
    agen = event_stream(topic, replay=True, heartbeat=0.05, last_event_id=last_event_id)
    try:
        while len(frames) < stop_after:
            raw = await asyncio.wait_for(agen.__anext__(), timeout=5.0)
            for line in raw.splitlines():
                if line.startswith("data: "):
                    frames.append(json.loads(line[6:]))
                elif line.startswith(":"):
                    frames.append({"type": "keep-alive"})
    finally:
        await agen.aclose()
    return frames


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# --------------------------------------------------------------------------- readiness


def test_readiness_probe_passes_on_a_healthy_database(clean_db):
    probe = readiness_probe()
    assert probe["ready"] is True
    assert probe["writable"] is True
    assert probe["schema_ok"] is True
    assert probe["missing_tables"] == []
    assert probe["journal_mode"] == "wal"
    assert probe["error"] is None


def test_readiness_probe_is_non_destructive(container):
    """The write probe must not create or remove any application data."""
    session = get_sessionmaker()()
    try:
        before = {
            "incidents": session.query(orm.Incident).count(),
            "events": session.query(orm.IncidentEvent).count(),
            "memories": session.query(orm.MemoryRecord).count(),
        }
    finally:
        session.close()

    assert readiness_probe()["writable"] is True

    session = get_sessionmaker()()
    try:
        after = {
            "incidents": session.query(orm.Incident).count(),
            "events": session.query(orm.IncidentEvent).count(),
            "memories": session.query(orm.MemoryRecord).count(),
        }
    finally:
        session.close()
    assert before == after


def test_required_tables_all_exist(container):
    from sqlalchemy import text

    from recallops.persistence.db import get_engine

    with get_engine().connect() as conn:
        found = {r[0] for r in conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))}
    assert set(REQUIRED_TABLES).issubset(found)


# --------------------------------------------------------------------------- HTTP health


def test_health_live_never_touches_the_database(client):
    """Liveness must stay green during a dependency outage, otherwise a service
    manager restarts a healthy API exactly when its database is having a bad day."""
    response = client.get("/health/live")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "alive"
    assert body["runtime"]["instance_id"]
    assert "ready" in body["runtime"]


def test_health_ready_reports_verified_checks(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    checks = body["checks"]
    assert checks["database_writable"] is True
    assert checks["schema_complete"] is True
    assert checks["missing_tables"] == []
    assert checks["journal_mode"] == "wal"
    assert checks["error"] is None
    assert body["runtime"]["ready"] is True


def test_health_ready_returns_503_when_the_database_is_unusable(client, monkeypatch):
    """A process that cannot write must be pulled out of rotation, not left to
    serve timeline writes that fail one request at a time."""
    import recallops.api.routes_health as routes_health

    monkeypatch.setattr(
        routes_health,
        "readiness_probe",
        lambda: {
            "writable": False,
            "schema_ok": False,
            "missing_tables": list(REQUIRED_TABLES),
            "journal_mode": None,
            "db_path": "locked",
            "error": "RuntimeError: database file is locked",
            "latency_ms": 0.1,
            "ready": False,
        },
    )
    response = client.get("/health/ready")
    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "not_ready"
    assert body["checks"]["database_writable"] is False
    assert "locked" in body["checks"]["error"]
    assert body["runtime"]["ready"] is False


def test_health_includes_runtime_identity_and_uptime(client):
    body = client.get("/health").json()
    rt = body["runtime"]
    assert rt["instance_id"]
    assert rt["pid"] > 0
    assert rt["uptime_s"] >= 0.0
    assert rt["started_at"]
    assert body["status"] == "ok"


def test_health_database_reports_durability_details(client):
    body = client.get("/health/database").json()
    durability = body["durability"]
    assert durability["writable"] is True
    assert durability["schema_ok"] is True
    assert durability["journal_mode"] == "wal"
    assert durability["db_path"]


def test_runtime_instance_is_stable_within_a_process():
    first = runtime.instance_id()
    assert runtime.instance_id() == first
    assert runtime.mark_started() == first


def test_root_advertises_liveness_and_readiness(client):
    body = client.get("/").json()
    assert body["endpoints"]["liveness"] == "/health/live"
    assert body["endpoints"]["readiness"] == "/health/ready"
    assert body["endpoints"]["timeline"] == "GET /api/incidents/{id}/events"
    assert body["runtime"]["instance_id"]


# --------------------------------------------------------------------------- SSE durability


def test_sse_frames_carry_an_id_for_resume():
    payload = sse_event({"type": "timeline", "event": {"id": "INC-A1-ev003", "seq": 3}}, event_id="INC-A1-ev003")
    assert payload.startswith("id: INC-A1-ev003\ndata: ")
    assert json.loads(payload.split("data: ", 1)[1].strip())["event"]["seq"] == 3


def test_seq_is_parsed_from_a_last_event_id():
    assert _seq_from_last_event_id("INC-A1-ev007") == 7
    assert _seq_from_last_event_id("INC-A1-ev012") == 12
    assert _seq_from_last_event_id("garbage") is None
    assert _seq_from_last_event_id(None) is None


def test_replay_from_db_survives_an_in_memory_restart(container):
    """The EventBus buffer is process-local. After a restart it is empty, so the
    stream must rebuild the timeline from ``incident_events`` or the operator
    sees a timeline with unexplained holes."""
    _seed_events("R1", 3)

    get_bus().clear("R1")  # simulates the restart: in-memory history is gone

    frames = replay_from_db("R1")
    assert [f["event"]["title"] for f in frames] == ["e0", "e1", "e2"]
    assert all(f["replayed"] is True for f in frames)
    assert [f["event"]["seq"] for f in frames] == [0, 1, 2]


def test_replay_from_db_resumes_after_a_last_event_id(container):
    _seed_events("R2", 5)
    get_bus().clear("R2")
    tail = replay_from_db("R2", after_seq=2)
    assert [f["event"]["title"] for f in tail] == ["e3", "e4"]


def test_stream_rebuilds_history_from_the_database_after_a_restart(container):
    """End-to-end: no in-memory history, a fresh stream still delivers every
    durable event, then announces which backend instance is now serving."""
    _seed_events("R3", 3)
    get_bus().clear("R3")

    frames = _run(_collect("R3", stop_after=5))
    titles = [f["event"]["title"] for f in frames if f.get("type") == "timeline"]
    assert titles == ["e0", "e1", "e2"]

    ready = [f for f in frames if f.get("type") == "ready"]
    assert ready, "stream must announce readiness"
    assert ready[0]["instance_id"] == runtime.instance_id()
    assert ready[0]["resumed_from"] is None


def test_stream_resumes_losslessly_from_last_event_id(container):
    """A reconnect after a drop must deliver only what the client missed."""
    _seed_events("R4", 4)
    get_bus().clear("R4")

    frames = _run(_collect("R4", last_event_id="R4-ev001", stop_after=4))
    titles = [f["event"]["title"] for f in frames if f.get("type") == "timeline"]
    assert titles == ["e2", "e3"]

    ready = [f for f in frames if f.get("type") == "ready"][0]
    assert ready["resumed_from"] == 1


def test_stream_does_not_replay_when_disabled(container):
    _seed_events("R5", 2)
    get_bus().clear("R5")

    async def collect_without_replay() -> list[dict[str, Any]]:
        frames: list[dict[str, Any]] = []
        agen = event_stream("R5", replay=False, heartbeat=0.05)
        try:
            while len(frames) < 1:
                raw = await asyncio.wait_for(agen.__anext__(), timeout=5.0)
                for line in raw.splitlines():
                    if line.startswith("data: "):
                        frames.append(json.loads(line[6:]))
        finally:
            await agen.aclose()
        return frames

    frames = _run(collect_without_replay())
    assert all(f.get("type") == "ready" for f in frames)
