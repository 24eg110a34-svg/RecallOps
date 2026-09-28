"""Local Hindsight mirror.

Purpose: keep the demo deterministic and the tests hermetic when a Hindsight
server is not reachable. It writes the *same* items, with the same document ids
and metadata, that would be sent to Hindsight - but into the application SQLite
database.

It is explicitly **not** Hindsight. ``MemoryHealth.mode`` reports
``local_hindsight`` and the UI renders "Local Hindsight mirror (Hindsight
unreachable)". Nothing in this file claims to be the real thing.

Retrieval is a transparent, explainable hybrid: BM25-style keyword scoring,
cosine overlap on token sets ("semantic" proxy), entity-graph expansion
(incident/service/cause links) and a recency term. Each hit records which
strategies contributed so the UI can show the search explanation.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, sessionmaker

from recallops.domain.enums import Durability, MemoryKind, MemoryMode
from recallops.domain.signals import tokenize
from recallops.memory.models import (
    MEMORY_MODE_LABELS,
    MemoryHealth,
    MemoryItem,
    MemoryQuery,
    MemoryRecallResult,
    MemoryWriteReceipt,
)
from recallops.memory.port import MemoryPort
from recallops.persistence.models import Incident, MemoryRecord

_STRATEGY_SEMANTIC = "local:semantic-overlap"
_STRATEGY_KEYWORD = "local:keyword-bm25"
_STRATEGY_GRAPH = "local:entity-graph"
_STRATEGY_TEMPORAL = "local:recency"


def _record_to_item(rec: MemoryRecord) -> MemoryItem:
    meta = dict(rec.meta or {})
    return MemoryItem(
        id=rec.id,
        kind=MemoryKind(rec.kind),
        title=rec.title,
        content=rec.content,
        service=rec.service,
        incident_id=rec.incident_id or str(meta.get("incident_ref") or ""),
        cause_id=rec.cause_id or "",
        action_id=rec.action_id,
        outcome=rec.outcome,
        helped=meta.get("helped"),
        expected_signal=str(meta.get("expected_signal") or ""),
        actual_outcome=str(meta.get("actual_outcome") or ""),
        lesson=str(meta.get("lesson") or ""),
        reusable_lesson=str(meta.get("reusable_lesson") or ""),
        durability=Durability(rec.durability),
        occurred_at=rec.created_at,
        source=MemoryMode(rec.source) if rec.source in {m.value for m in MemoryMode} else MemoryMode.LOCAL_HINDSIGHT,
        external_id=rec.external_id,
        memory_type=rec.memory_type if rec.memory_type in {"world", "experience", "observation"} else "world",
        tags=list(rec.tags or []),
        entities=list(rec.entities or []),
        meta=meta,
    )


def _item_to_record(item: MemoryItem) -> MemoryRecord:
    meta = dict(item.meta)
    meta["helped"] = item.helped
    meta["expected_signal"] = item.expected_signal
    meta["actual_outcome"] = item.actual_outcome
    meta["lesson"] = item.lesson
    meta["reusable_lesson"] = item.reusable_lesson
    return MemoryRecord(
        id=item.id,
        incident_id=item.incident_id or None,
        kind=item.kind.value,
        durability=item.durability.value,
        title=item.title[:255],
        content=item.content,
        service=item.service,
        cause_id=item.cause_id,
        action_id=item.action_id,
        outcome=item.outcome,
        score=item.score,
        source=item.source.value,
        external_id=item.external_id,
        memory_type=item.memory_type,
        entities=item.entities,
        tags=item.tags,
        meta=meta,
        created_at=item.occurred_at or datetime.now(timezone.utc),
    )


class LocalMemoryAdapter(MemoryPort):
    """SQLite-backed mirror of the memory bank."""

    name = "local_hindsight"
    mode_label = MEMORY_MODE_LABELS[MemoryMode.LOCAL_HINDSIGHT.value]

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory

    # ------------------------------------------------------------------ session
    def _session(self) -> Session:
        """Open a session, accepting a factory or a sessionmaker."""
        candidate = self._session_factory()
        return candidate() if isinstance(candidate, sessionmaker) else candidate

    # ------------------------------------------------------------------ recall
    def recall(self, query: MemoryQuery, scope: str | None = None) -> MemoryRecallResult:
        started = time.perf_counter()
        session = self._session()
        try:
            rows = list(session.execute(select(MemoryRecord)).scalars())
        finally:
            session.close()

        items = [_record_to_item(r) for r in rows]
        if query.kinds:
            items = [i for i in items if i.kind in query.kinds]
        # NB: tags are *not* a hard filter here. A payment-api failure pattern is
        # legitimately useful precedent for orders-api, so scope is decided by the
        # service/cause boosts and lexical scoring below, not by an AND on tags.
        if query.exclude_incident_id:
            items = [i for i in items if i.incident_id != query.exclude_incident_id]

        scored = self._score(items, query)
        limit = max(1, query.limit)
        top = [s for s in scored[:limit] if s[1].score >= query.min_score]

        strategy_summary: Counter[str] = Counter()
        for _, it in top:
            strategy_summary.update(it.strategy_hits)

        for _, it in top:
            it.why = self._why(it, query)
            it.source = MemoryMode.LOCAL_HINDSIGHT

        return MemoryRecallResult(
            items=[it for _, it in top],
            mode=MemoryMode.LOCAL_HINDSIGHT,
            provider=self.name,
            total_found=len(items),
            relevant_count=len(top),
            latency_ms=int((time.perf_counter() - started) * 1000),
            strategy_summary=dict(strategy_summary),
            query=query.text,
            detail="local_mirror",
        )

    def _score(self, items: Sequence[MemoryItem], query: MemoryQuery) -> list[tuple[float, MemoryItem]]:
        if not items:
            return []
        docs = [Counter(tokenize(i.searchable_text())) for i in items]
        n_docs = len(docs)
        df: Counter[str] = Counter()
        for toks in docs:
            df.update(set(toks))
        q_tokens = tokenize(query.text)
        if not q_tokens:
            return [(0.0, i) for i in items]

        avg_len = sum(sum(d.values()) for d in docs) / n_docs or 1.0
        k1, b = 1.4, 0.72
        q_weights = {t: math.log(1 + (n_docs - df[t] + 0.5) / (df[t] + 0.5)) for t in q_tokens if df[t] > 0}

        results: list[tuple[float, MemoryItem]] = []
        for item, doc in zip(items, docs):
            strategies: list[str] = []
            score = 0.0

            # 1) keyword / BM25
            bm25 = 0.0
            for term, weight in q_weights.items():
                tf = doc.get(term, 0)
                if not tf:
                    continue
                norm = tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(doc) / avg_len))
                bm25 += weight * norm
            if bm25 > 0:
                score += min(1.0, bm25 / 6.0) * 0.45
                strategies.append(_STRATEGY_KEYWORD)

            # 2) semantic proxy: cosine over token sets (catches paraphrase)
            if doc:
                inter = len(q_tokens & set(doc))
                if inter:
                    cos = inter / math.sqrt(len(q_tokens) * len(doc))
                    score += min(1.0, cos * 1.6) * 0.3
                    strategies.append(_STRATEGY_SEMANTIC)

            # 3) graph: shared entity / scope edges
            graph = 0.0
            if query.service and item.service and query.service == item.service:
                graph += 0.18
            if query.cause_ids and item.cause_id and item.cause_id in query.cause_ids:
                graph += 0.14
            if set(query.tags) & {t.lower() for t in item.tags}:
                graph += 0.06
            if graph:
                score += graph
                strategies.append(_STRATEGY_GRAPH)

            # 3b) action outcomes are the memories that change behaviour: a
            # recorded failure on a matching symptom is the most actionable
            # precedent we have, so give it precedence over plain background.
            if item.kind is MemoryKind.ACTION_OUTCOME:
                score += -0.18 if item.helped is False else 0.10
            elif item.kind is MemoryKind.SERVICE_PATTERN:
                score += 0.05

            # 4) temporal: newer memories break ties
            if item.occurred_at:
                age_days = max(0.0, (datetime.now(timezone.utc) - _aware(item.occurred_at)).total_seconds() / 86400)
                recency = 0.05 * math.exp(-age_days / 45.0)
                score += recency
                strategies.append(_STRATEGY_TEMPORAL)

            if item.durability is not Durability.DURABLE:
                score *= 0.5
            if query.exclude_incident_id and item.incident_id == query.exclude_incident_id:
                score = 0.0

            item.score = round(min(1.0, score), 4)
            item.strategy_hits = strategies
            if item.score > 0:
                results.append((item.score, item))
        results.sort(key=lambda p: (-p[0], p[1].id))
        return results

    def _why(self, item: MemoryItem, query: MemoryQuery) -> str:
        bits: list[str] = []
        if query.service and item.service == query.service:
            bits.append(f"same service ({item.service})")
        if query.cause_ids and item.cause_id in query.cause_ids:
            bits.append(f"same cause family ({item.cause_id})")
        if item.incident_id:
            bits.append(f"previous incident {item.incident_id}")
        if item.kind is MemoryKind.ACTION_OUTCOME and item.helped is False:
            bits.append("records a failed action")
        if not bits:
            bits.append("matched the retrieval query")
        return "; ".join(bits)

    # ------------------------------------------------------------------ retain
    def retain(self, items: list[MemoryItem], scope: str | None = None) -> MemoryWriteReceipt:
        session = self._session()
        written_ids: list[str] = []
        rejected: list[dict[str, str]] = []
        details: list[dict[str, Any]] = []
        try:
            existing = {r.id for r in session.execute(select(MemoryRecord)).scalars()}
            known_incidents = {row[0] for row in session.execute(select(Incident.id)).all()}
            for item in items:
                if item.id in existing:
                    details.append({"id": item.id, "status": "duplicate", "detail": "already retained (idempotent)"})
                    continue
                item.source = MemoryMode.LOCAL_HINDSIGHT
                if item.occurred_at is None:
                    item.occurred_at = datetime.now(timezone.utc)
                record = _item_to_record(item)
                if record.incident_id and record.incident_id not in known_incidents:
                    # The mirror keeps provenance even when the incident row is gone.
                    record.meta = {**(record.meta or {}), "incident_ref": record.incident_id}
                    record.incident_id = None
                session.add(record)
                existing.add(item.id)
                written_ids.append(item.id)
                details.append({"id": item.id, "status": "written", "kind": item.kind.value})
            session.commit()
        except Exception as exc:  # noqa: BLE001
            session.rollback()
            return MemoryWriteReceipt(
                written=0,
                rejected=len(items),
                mode=MemoryMode.LOCAL_HINDSIGHT,
                degraded=True,
                error={"kind": "write_failed", "message": str(exc)[:300]},
            )
        finally:
            session.close()
        return MemoryWriteReceipt(written=len(written_ids), ids=written_ids, mode=MemoryMode.LOCAL_HINDSIGHT, details=details)

    # ------------------------------------------------------------------ introspection
    def list_memories(self, *, limit: int = 100, service: str | None = None, kind: str | None = None) -> dict[str, Any]:
        session = self._session()
        try:
            stmt = select(MemoryRecord).order_by(MemoryRecord.created_at.desc()).limit(limit)
            if service:
                stmt = stmt.where(MemoryRecord.service == service)
            if kind:
                stmt = stmt.where(MemoryRecord.kind == kind)
            rows = list(session.execute(stmt).scalars())
        finally:
            session.close()
        return {
            "supported": True,
            "count": len(rows),
            "items": [
                {
                    **{
                        k: v
                        for k, v in {
                            "id": r.id,
                            "kind": r.kind,
                            "title": r.title,
                            "service": r.service,
                            "incident_id": r.incident_id,
                            "cause_id": r.cause_id,
                            "outcome": r.outcome,
                            "durability": r.durability,
                            "tags": r.tags,
                            "created_at": r.created_at.isoformat() if r.created_at else None,
                            "content": r.content,
                        }.items()
                    }
                }
                for r in rows
            ],
        }

    def stats(self) -> dict[str, Any]:
        session = self._session()
        try:
            rows = list(session.execute(select(MemoryRecord)).scalars())
        finally:
            session.close()
        by_kind: Counter[str] = Counter(r.kind for r in rows)
        by_service: Counter[str] = Counter(r.service for r in rows if r.service)
        failed = [r for r in rows if r.outcome and r.outcome in {"temporary_improvement", "hurt", "no_effect"}]
        return {
            "total": len(rows),
            "by_kind": dict(by_kind),
            "by_service": dict(by_service),
            "durable": sum(1 for r in rows if r.durability == Durability.DURABLE.value),
            "episodic": sum(1 for r in rows if r.durability != Durability.DURABLE.value),
            "failed_action_memories": len(failed),
            "incidents_represented": len({r.incident_id for r in rows if r.incident_id}),
        }

    def graph(self, *, root_incident_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        from recallops.memory.graph import build_graph

        session = self._session()
        try:
            rows = list(session.execute(select(MemoryRecord).limit(limit)).scalars())
        finally:
            session.close()
        return build_graph([_record_to_item(r) for r in rows], root_incident_id=root_incident_id, source=self.name)

    def health(self) -> MemoryHealth:
        started = time.perf_counter()
        stats: dict[str, Any] = {}
        detail = ""
        state = "connected"
        try:
            stats = self.stats()
            detail = f"Local memory mirror active with {stats.get('total', 0)} memories."
        except Exception as exc:  # noqa: BLE001
            state, detail = "unavailable", f"Local memory mirror error: {exc}"  # type: ignore[assignment]
        return MemoryHealth(
            state=state,  # type: ignore[arg-type]
            mode=MemoryMode.LOCAL_HINDSIGHT,
            mode_label=self.mode_label,
            provider=self.name,
            detail=detail,
            latency_ms=int((time.perf_counter() - started) * 1000),
            stats=stats,
        )

    def reset(self) -> None:
        session = self._session()
        try:
            session.execute(delete(MemoryRecord))
            session.commit()
        finally:
            session.close()


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


__all__ = ["LocalMemoryAdapter"]
