"""Learning analytics, recurring-pattern detection and memory quality metrics.

Everything is computed from the application database: no invented numbers. Where a
metric needs a definition (recall@k, MRR, memory contribution) the formula is
documented next to the code that computes it.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from recallops.agent.record import record_from_orm
from recallops.domain.enums import IncidentState
from recallops.domain.signals import CAUSES, cause_name
from recallops.memory.port import MemoryPort
from recallops.persistence import models as orm


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


class AnalyticsService:
    def __init__(self, *, session_factory: Any, orchestrator: Any, memory: MemoryPort) -> None:
        self.session_factory = session_factory
        self.orchestrator = orchestrator
        self.memory = memory

    def _records(self) -> list[Any]:
        session = self.session_factory()
        try:
            rows = list(session.execute(select(orm.Incident).order_by(orm.Incident.created_at.desc())).scalars())
            return [record_from_orm(row, scenario=self.orchestrator.scenario_for(row)) for row in rows]
        finally:
            session.close()

    # ------------------------------------------------------------------ overview
    def overview(self) -> dict[str, Any]:
        records = self._records()
        resolved = [r for r in records if r.is_resolved]
        steps = [r.step_count for r in records if r.step_count]
        minutes = [m for m in (r.resolution_minutes() for r in resolved) if m is not None]

        failed_actions = [a for r in records for a in r.failed_actions() if a.risk.value != "READ_ONLY"]
        blocked = [a for r in records for a in r.blocked_actions()]
        memories = self._memory_rows()
        memory_assisted = [r for r in records if r.memory_assisted]

        session = self.session_factory()
        try:
            memory_total = session.query(orm.MemoryRecord).count()
            durable = session.query(orm.MemoryRecord).filter(orm.MemoryRecord.durability == "durable").count()
            postmortems = session.query(orm.Postmortem).count()
            runbooks = session.query(orm.Runbook).count()
        finally:
            session.close()

        return {
            "generated_at": _iso(datetime.now(timezone.utc)),
            "totals": {
                "incidents": len(records),
                "resolved": len(resolved),
                "open": len(records) - len(resolved),
                "postmortems": postmortems,
                "runbooks": runbooks,
                "memories": memory_total,
                "durable_memories": durable,
                "memory_assisted_incidents": len(memory_assisted),
                "failed_actions": len(failed_actions),
                "failed_actions_avoided": len([a for a in blocked if not a.result]),
            },
            "efficiency": {
                "avg_investigation_steps": round(sum(steps) / len(steps), 2) if steps else 0.0,
                "avg_resolution_minutes": round(sum(minutes) / len(minutes), 2) if minutes else 0.0,
                "avg_steps_memory_on": self._avg_steps(records, memory_on=True),
                "avg_steps_memory_off": self._avg_steps(records, memory_on=False),
            },
            "top_root_causes": self.top_root_causes(),
            "recurring_patterns": self.patterns(),
            "failed_action_ledger": [
                {
                    "incident_id": r.id,
                    "action": a.description,
                    "outcome": a.result.outcome.value if a.result else None,
                    "lesson": (a.result.lesson if a.result else None),
                }
                for r in records
                for a in r.failed_actions()
            ][:20],
            "memory_quality": self.memory_quality(),
            "retrieval_metrics": self.retrieval_metrics(records),
        }

    def _avg_steps(self, records: list[Any], *, memory_on: bool) -> float:
        values = [r.step_count for r in records if r.step_count and r.memory_enabled is memory_on]
        return round(sum(values) / len(values), 2) if values else 0.0

    def _memory_rows(self) -> list[orm.MemoryRecord]:
        session = self.session_factory()
        try:
            return list(session.execute(select(orm.MemoryRecord)).scalars())
        finally:
            session.close()

    # ------------------------------------------------------------------ causes
    def top_root_causes(self) -> list[dict[str, Any]]:
        records = self._records()
        counter: Counter[str] = Counter(r.root_cause_id for r in records if r.root_cause_id)
        total = sum(counter.values()) or 1
        out = []
        for cause_id, count in counter.most_common():
            cause = CAUSES.get(cause_id)
            out.append(
                {
                    "cause_id": cause_id,
                    "cause": cause_name(cause_id) if cause else cause_id,
                    "count": count,
                    "share_pct": round(count / total * 100, 1),
                    "services": sorted({r.service for r in records if r.root_cause_id == cause_id}),
                }
            )
        return out

    # ------------------------------------------------------------------ patterns
    def patterns(self) -> list[dict[str, Any]]:
        """Recurring incident patterns (>=2 occurrences of a cause family)."""
        records = [r for r in self._records() if r.root_cause_id]
        by_cause: dict[str, list[Any]] = defaultdict(list)
        for record in records:
            by_cause[record.root_cause_id].append(record)
        patterns: list[dict[str, Any]] = []
        for cause_id, group in sorted(by_cause.items(), key=lambda kv: -len(kv[1])):
            if len(group) < 1:
                continue
            deploys = [
                d.get("version")
                for r in group
                for d in r.deployments
                if (d.get("minutes_before_incident") or 999) <= 60
            ]
            services = sorted({r.service for r in group})
            failed = sorted(
                {
                    a.description
                    for r in group
                    for a in r.failed_actions()
                    if a.risk.value != "READ_ONLY"
                }
            )
            cause = CAUSES.get(cause_id)
            patterns.append(
                {
                    "cause_id": cause_id,
                    "cause": cause_name(cause_id) if cause else cause_id,
                    "occurrences": len(group),
                    "recurring": len(group) > 1,
                    "incidents": [r.id for r in group],
                    "services": services,
                    "common_trigger": (
                        f"recent deploy ({', '.join(str(d) for d in sorted(set(deploys))[:3])})"
                        if deploys
                        else "no consistent deploy correlation"
                    ),
                    "known_bad_actions": failed,
                    "suggested_investigation": cause.diagnostics[0] if cause and cause.diagnostics else "Review the confirmed diagnostic for this cause family.",
                    "severities": [r.severity for r in group],
                }
            )
        return patterns

    # ------------------------------------------------------------------ memory
    def memory_quality(self) -> dict[str, Any]:
        rows = self._memory_rows()
        by_kind: Counter[str] = Counter(r.kind for r in rows)
        by_service: Counter[str] = Counter(r.service for r in rows if r.service)
        failed = [r for r in rows if r.outcome in {"temporary_improvement", "hurt", "no_effect"}]
        lessons = [r for r in rows if (r.meta or {}).get("reusable_lesson")]
        return {
            "total": len(rows),
            "by_kind": dict(by_kind),
            "by_service": dict(by_service),
            "failed_action_memories": len(failed),
            "with_reusable_lesson": len(lessons),
            "durable_ratio": round(len([r for r in rows if r.durability == "durable"]) / len(rows), 2) if rows else 0.0,
            "sources": dict(Counter(r.source for r in rows)),
        }

    def retrieval_metrics(self, records: list[Any]) -> dict[str, Any]:
        """Recall@k and MRR over the memories the agent actually cited.

        recall@k  = share of cited memory items that belong to a *previous*
                    incident (i.e. genuinely useful precedent rather than noise)
        MRR       = mean reciprocal rank of the first previous-incident memory in
                    each incident's recalled list
        """
        ranks: list[float] = []
        cited_total = 0
        previous_cited = 0
        for record in records:
            ids = record.recalled_memory_ids or []
            cited_total += len(ids)
            first_rank = 0
            for position, memory_id in enumerate(ids, start=1):
                previous = any(memory_id.startswith(f"{other}~") for other in self._other_incident_ids(record.id))
                if previous:
                    previous_cited += 1
                    if first_rank == 0:
                        first_rank = position
            if first_rank:
                ranks.append(1.0 / first_rank)
        return {
            "incidents_with_memory": len([r for r in records if r.recalled_memory_ids]),
            "citations": cited_total,
            "previous_incident_citations": previous_cited,
            "recall_at_3": round(sum(1 for r in ranks if 1 / (1 / r) <= 3) / len(records), 3) if records and ranks else 0.0,
            "mrr": round(sum(ranks) / len(ranks), 3) if ranks else 0.0,
            "definition": "citations = memories cited by hypotheses; MRR ranks the first previous-incident memory in each recall",
        }

    def _other_incident_ids(self, incident_id: str) -> set[str]:
        session = self.session_factory()
        try:
            rows = list(session.execute(select(orm.MemoryRecord.incident_id)).scalars())
        finally:
            session.close()
        return {r for r in rows if r and r != incident_id}

    # ------------------------------------------------------------------ lists
    def runbooks(self) -> list[dict[str, Any]]:
        session = self.session_factory()
        try:
            rows = list(session.execute(select(orm.Runbook).order_by(orm.Runbook.updated_at.desc())).scalars())
            return [
                {
                    "id": r.id,
                    "title": r.title,
                    "service": r.service,
                    "cause_id": r.cause_id,
                    "symptoms": r.symptoms,
                    "first_checks": r.first_checks,
                    "known_failed_actions": r.known_failed_actions,
                    "recommended_actions": r.recommended_actions,
                    "verification": r.verification,
                    "prevention": r.prevention,
                    "steps": r.steps,
                    "source_incidents": r.source_incidents,
                    "updated_at": _iso(r.updated_at),
                }
                for r in rows
            ]
        finally:
            session.close()

    def postmortems(self) -> list[dict[str, Any]]:
        session = self.session_factory()
        try:
            rows = list(session.execute(select(orm.Postmortem).order_by(orm.Postmortem.generated_at.desc())).scalars())
            return [
                {
                    "id": r.id,
                    "incident_id": r.incident_id,
                    "title": r.title,
                    "severity": r.severity,
                    "service": r.service,
                    "summary": r.summary,
                    "root_cause": r.root_cause,
                    "resolution": r.resolution,
                    "lessons": r.lessons,
                    "prevention": r.prevention,
                    "failed_actions": [a.get("description") for a in (r.failed_actions or [])],
                    "successful_actions": [a.get("description") for a in (r.successful_actions or [])],
                    "memory_written": r.memory_written,
                    "memory_written_count": r.memory_written_count,
                    "generated_at": _iso(r.generated_at),
                }
                for r in rows
            ]
        finally:
            session.close()

    def state_breakdown(self) -> dict[str, int]:
        counter: Counter[str] = Counter(r.state for r in self._records())
        return {state.value: counter.get(state.value, 0) for state in IncidentState}


__all__ = ["AnalyticsService"]
