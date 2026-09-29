"""Timeline events + Server-Sent Events.

The timeline is stored in ``incident_events`` (audit + replay) and published on an
in-process bus that ``GET /api/incidents/{id}/stream`` subscribes to. SSE is used
rather than WebSockets because the flow is one-directional and survives proxies.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, AsyncIterator

from recallops.domain.enums import TimelinePhase
from recallops.domain.models import TimelineEvent
from recallops.persistence import models as orm
from recallops.services import runtime

logger = logging.getLogger("recallops.events")


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


def sse_event(data: dict[str, Any], *, event: str | None = None, event_id: str | None = None) -> str:
    body = json.dumps(data, default=str)
    parts: list[str] = []
    if event_id:
        # Standard SSE id: a browser EventSource replays it as `Last-Event-ID`
        # on reconnect, which is what makes reconnection lossless.
        parts.append(f"id: {event_id}")
    if event:
        parts.append(f"event: {event}")
    parts.append(f"data: {body}")
    return "\n".join(parts) + "\n\n"


def _event_id(payload: dict[str, Any]) -> str | None:
    """Derive a stable SSE id from a timeline payload."""
    event = payload.get("event")
    if isinstance(event, dict) and event.get("seq") is not None:
        return f"{event.get('id')}"
    return None


def replay_from_db(topic: str, *, after_seq: int | None = None, limit: int = 400) -> list[dict[str, Any]]:
    """Rebuild recent SSE frames from the durable ``incident_events`` table.

    The in-process :class:`EventBus` buffer is intentionally small and is lost on
    restart. Without this, a client that reconnects after the API bounced would
    silently miss every event that happened during the outage - the timeline
    would look complete but have holes. The database is the source of truth, so
    a reconnect after a restart is served from it.
    """
    from recallops.persistence import models as orm
    from recallops.persistence.db import get_sessionmaker

    session = get_sessionmaker()()
    try:
        query = session.query(orm.IncidentEvent).filter(orm.IncidentEvent.incident_id == topic)
        if after_seq is not None:
            query = query.filter(orm.IncidentEvent.seq > after_seq)
        rows = query.order_by(orm.IncidentEvent.seq.desc()).limit(limit).all()
        events = list(reversed(rows))
        return [
            {
                "type": "timeline",
                "replayed": True,
                "event": {
                    "id": row.id,
                    "incident_id": row.incident_id,
                    "seq": row.seq,
                    "ts": row.ts,
                    "phase": row.phase,
                    "title": row.title,
                    "detail": row.detail,
                    "actor": row.actor,
                    "meta": row.meta or {},
                },
            }
            for row in events
        ]
    except Exception as exc:  # noqa: BLE001 - never break the stream on replay
        logger.warning("SSE durable replay failed for %s: %s", topic, exc)
        return []
    finally:
        session.close()


def _seq_from_last_event_id(last_event_id: str | None) -> int | None:
    """``INC-A1-ev007`` -> ``7``. Returns None when the header is unusable."""
    if not last_event_id:
        return None
    tail = last_event_id.rsplit("ev", 1)[-1]
    try:
        return int(tail)
    except ValueError:
        return None


async def event_stream(
    topic: str,
    *,
    replay: bool = True,
    heartbeat: float = 15.0,
    last_event_id: str | None = None,
) -> AsyncIterator[str]:
    """SSE stream for one topic, with replay of history and heartbeats.

    Reconnection is lossless in two ways:

    * ``Last-Event-ID`` (sent automatically by ``EventSource``) resumes exactly
      after the last frame the client processed;
    * if the client reconnects to a *different* process - a restart, a rolling
      deploy - the in-memory buffer no longer has its history, so the stream
      falls back to the durable table and announces the new ``instance_id`` so
      the UI knows it must resync.
    """
    queue = _bus.subscribe(topic)
    resumed_from: int | None = None
    try:
        if replay:
            resumed_from = _seq_from_last_event_id(last_event_id)
            frames: list[dict[str, Any]] = []
            if resumed_from is not None:
                # Precise resume: everything after the client's last frame.
                frames = replay_from_db(topic, after_seq=resumed_from)
                if not frames:
                    frames = [p for p in _bus.history(topic) if (p.get("event") or {}).get("seq", -1) > resumed_from]
            else:
                frames = _bus.history(topic)
            if not frames:
                # No in-memory history (fresh process): rebuild from the database.
                frames = replay_from_db(topic)
            for payload in frames:
                yield sse_event(payload, event_id=_event_id(payload))
        yield sse_event(
            {
                "type": "ready",
                "topic": topic,
                "instance_id": runtime.instance_id(),
                "resumed_from": resumed_from,
                "replayed": bool(replay),
                "server_time": time.time(),
            },
            event="ready",
        )
        while True:
            try:
                payload = await asyncio.wait_for(queue.get(), timeout=heartbeat)
            except asyncio.TimeoutError:
                yield ": keep-alive\n\n"
                continue
            yield sse_event(payload, event_id=_event_id(payload))
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
    "replay_from_db",
    "sse_event",
]
