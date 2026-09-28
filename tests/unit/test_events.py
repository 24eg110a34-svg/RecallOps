"""Timeline events and SSE payload format."""

from __future__ import annotations

import json

from recallops.domain.enums import TimelinePhase
from recallops.services.events import EventBus, next_seq, record_event, sse_event


def test_event_bus_publishes_per_topic():
    bus = EventBus()
    queue = bus.subscribe("INC-A1")
    bus.publish("INC-A1", {"type": "timeline", "event": {"title": "hello"}})
    assert queue.get_nowait()["event"]["title"] == "hello"
    assert bus.history("INC-A1")[0]["event"]["title"] == "hello"
    bus.unsubscribe("INC-A1", queue)
    assert bus.topics() == ["INC-A1"]  # history is kept for replay; only the queue is dropped


def test_sse_event_format():
    payload = sse_event({"type": "ready", "topic": "INC-A1"}, event="ready")
    assert payload.startswith("event: ready\ndata: ")
    assert payload.endswith("\n\n")
    body = payload.split("data: ", 1)[1].strip()
    assert json.loads(body)["topic"] == "INC-A1"


def test_record_event_increments_sequence(clean_db):
    from recallops.persistence.db import get_sessionmaker
    from recallops.persistence import models as orm

    session = get_sessionmaker()()
    try:
        session.add(orm.Incident(id="X", scenario_id="INC-A1", service="s", title="t"))
        session.flush()
        first = record_event(session, incident_id="X", phase=TimelinePhase.INCIDENT, title="a")
        second = record_event(session, incident_id="X", phase=TimelinePhase.EVIDENCE, title="b")
        assert second.seq == first.seq + 1
        assert next_seq(session, "X") == second.seq + 1
        session.rollback()
    finally:
        session.close()
