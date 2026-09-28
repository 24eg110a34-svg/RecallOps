"""Timeline events + Server-Sent Events.

The timeline is stored in ``incident_events`` (audit + replay) and published on an
in-process bus that ``GET /api/incidents/{id}/stream`` subscribes to. SSE is used
rather than WebSockets because the flow is one-directional and survives proxies.
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from recallops.domain.enums import TimelinePhase
from recallops.domain.models import TimelineEvent
from recallops.persistence import models as orm


def next_seq(session: Any, incident_id: str) -> int:
    rows = session.query(orm.IncidentEvent.seq).filter(orm.IncidentEvent.incident_id == incident_id).all()
    return (max((r[0] or 0) for r in rows) + 1) if rows else 0


def record_event(
    session: Any,
    *,
    incident_id: str,
    phase: TimelinePhase | str,
    title: str,
    detail: str = "",
    actor: str = "system",
    meta: dict[str, Any] | None = None,
    ts: datetime | None = None,
) -> TimelineEvent:
    seq = next_seq(session, incident_id)
    when = ts or datetime.now(timezone.utc)
    event = orm.IncidentEvent(
        id=f"{incident_id}-ev{seq:03d}",
        incident_id=incident_id,
        seq=seq,
        ts=when,
        phase=str(phase),
        title=title[:500],
        detail=detail or "",
        actor=actor,
        meta=meta or {},
    )
    session.add(event)
    session.flush()  # make the sequence visible to the next event in this transaction
    return TimelineEvent(
        id=event.id,
        incident_id=incident_id,
        seq=seq,
        ts=when,
        phase=phase,  # type: ignore[arg-type]
        title=title,
        detail=detail,
        actor=actor,
        meta=meta or {},
    )


class EventBus:
    """Minimal in-process pub/sub with per-incident topics and a replay buffer."""

    def __init__(self, history: int = 400) -> None:
        self._topics: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._history: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._history_limit = history

    def subscribe(self, topic: str) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=256)
        self._topics[topic].add(queue)
        return queue

    def unsubscribe(self, topic: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._topics.get(topic, set()).discard(queue)
        if not self._topics.get(topic):
            self._topics.pop(topic, None)

    def publish(self, topic: str, payload: dict[str, Any]) -> None:
        self._history[topic].append(payload)
        if len(self._history[topic]) > self._history_limit:
            self._history[topic] = self._history[topic][-self._history_limit :]
        for queue in list(self._topics.get(topic, set())):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - slow consumer
                pass

    def history(self, topic: str) -> list[dict[str, Any]]:
        return list(self._history.get(topic, []))

    def clear(self, topic: str | None = None) -> None:
        if topic is None:
            self._topics.clear()
            self._history.clear()
        else:
            self._history.pop(topic, None)

    def topics(self) -> list[str]:
        return sorted(set(self._topics) | set(self._history))


_bus = EventBus()


def get_bus() -> EventBus:
    return _bus


def publish_event(topic: str, event: TimelineEvent) -> None:
    _bus.publish(topic, {"type": "timeline", "event": event.model_dump(mode="json")})


def publish_payload(topic: str, payload: dict[str, Any]) -> None:
    _bus.publish(topic, payload)


def sse_event(data: dict[str, Any], *, event: str | None = None) -> str:
    body = json.dumps(data, default=str)
    prefix = f"event: {event}\n" if event else ""
    return f"{prefix}data: {body}\n\n"


async def event_stream(topic: str, *, replay: bool = True, heartbeat: float = 15.0) -> AsyncIterator[str]:
    """SSE stream for one topic, with replay of history and heartbeats."""
    queue = _bus.subscribe(topic)
    try:
        if replay:
            for payload in _bus.history(topic):
                yield sse_event(payload)
        yield sse_event({"type": "ready", "topic": topic}, event="ready")
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield sse_event(payload)
    finally:
        _bus.unsubscribe(topic, queue)


__all__ = [
    "EventBus",
    "event_stream",
    "get_bus",
    "next_seq",
    "publish_event",
    "publish_payload",
    "record_event",
    "sse_event",
]
