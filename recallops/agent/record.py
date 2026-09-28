"""A detached, testable view of an incident.

The agent modules work on this dataclass instead of ORM rows, so they can be unit
tested with plain objects and so the API layer has one place that converts
database state into domain state.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence

from recallops.agent.scenarios import Scenario
from recallops.domain.enums import ActionStatus, EvidenceKind, IncidentState, MemoryKind, Outcome
from recallops.domain.models import (
    ActionResult,
    ActionSpec,
    CauseHypothesis,
    EvidenceRef,
    ImpactAssessment,
    SeverityAssessment,
    SeverityFactor,
    TimelineEvent,
    WhatIfResult,
)
from recallops.memory.models import MemoryItem
from recallops.persistence import models as orm


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def evidence_from_orm(row: orm.EvidenceEvent) -> EvidenceRef:
    return EvidenceRef(
        id=row.id,
        kind=row.kind,
        source=row.source,
        title=row.title,
        detail=row.detail,
        ts=_aware(row.ts),
        stage=row.stage or None,
        signal_hints=list(row.signal_hints or []),
        raw=dict(row.raw or {}),
        untrusted=bool(row.untrusted),
        redacted=bool(row.redacted),
    )


def hypothesis_from_orm(row: orm.Hypothesis) -> CauseHypothesis:
    return CauseHypothesis(
        id=row.id,
        cause=row.cause,
        category=row.category,
        confidence=row.confidence,
        confidence_basis=row.confidence_basis or "",
        rationale=row.rationale or "",
        next_diagnostic=row.next_diagnostic or "",
        supporting=[EvidenceRef.model_validate(e) for e in (row.supporting or [])],
        contradicting=[EvidenceRef.model_validate(e) for e in (row.contradicting or [])],
        precedent=list(row.precedent or []),
        memory_links=[m for m in (row.memory_links or [])],
        memory_contribution=row.memory_contribution or 0.0,
        signals=list(row.signals or []),
        confirmed=bool(row.confirmed),
        rejected=bool(row.rejected),
        rejected_reason=row.rejected_reason,
        origin=row.origin or "rule_engine",
        rank=row.rank or 0,
    )


def action_from_orm(row: orm.ActionAttempt) -> ActionSpec:
    return ActionSpec(
        id=row.id,
        incident_id=row.incident_id,
        description=row.description,
        type=row.type,
        risk=row.risk,
        reason=row.reason or "",
        expected_signal=row.expected_signal or "",
        requires_confirmation=bool(row.requires_confirmation),
        status=row.status,
        tool=row.tool or "",
        params=dict(row.params or {}),
        reversible=bool(row.reversible),
        production_impact=bool(row.production_impact),
        data_loss_risk=bool(row.data_loss_risk),
        blocked_reason=row.blocked_reason,
        memory_warnings=list(row.memory_warnings or []),
        safety_notes=list(row.safety_notes or []),
        hypothesis_id=row.hypothesis_id,
        what_if=WhatIfResult.model_validate(row.what_if) if row.what_if else None,
        result=ActionResult.model_validate(row.result) if row.result else None,
        created_at=_aware(row.created_at) or datetime.now(timezone.utc),
        decided_at=_aware(row.decided_at),
        step_index=row.step_index or 0,
    )


def event_from_orm(row: orm.IncidentEvent) -> TimelineEvent:
    return TimelineEvent(
        id=row.id,
        incident_id=row.incident_id,
        seq=row.seq or 0,
        ts=_aware(row.ts) or datetime.now(timezone.utc),
        phase=row.phase,
        title=row.title,
        detail=row.detail or "",
        actor=row.actor or "system",
        meta=dict(row.meta or {}),
    )


@dataclass
class IncidentRecord:
    id: str
    scenario_id: str
    service: str
    title: str
    severity: str
    state: str
    symptom: str = ""
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    detected_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None
    root_cause: str = ""
    root_cause_id: str = ""
    resolution: str = ""
    step_count: int = 0
    confirmed_step: int = 0
    memory_mode: str = ""
    memory_enabled: bool = True
    memory_assisted: bool = False
    memory_contribution: float = 0.0
    top_hypothesis_confidence: float = 0.0
    top_hypothesis_cause_id: str = ""
    blocked_action_count: int = 0
    severity_assessment: SeverityAssessment | None = None
    impact: ImpactAssessment | None = None
    evidence: list[EvidenceRef] = field(default_factory=list)
    hypotheses: list[CauseHypothesis] = field(default_factory=list)
    actions: list[ActionSpec] = field(default_factory=list)
    events: list[TimelineEvent] = field(default_factory=list)
    memories: list[MemoryItem] = field(default_factory=list)
    deployments: list[dict[str, Any]] = field(default_factory=list)
    metrics: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    dependencies: list[dict[str, Any]] = field(default_factory=list)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    stage_id: str = ""
    sim_metrics: dict[str, float] = field(default_factory=dict)
    recalled_memory_ids: list[str] = field(default_factory=list)
    scenario: Scenario | None = None
    conflicts: list[dict[str, Any]] = field(default_factory=list)

    # ------------------------------------------------------------------ helpers
    @property
    def is_resolved(self) -> bool:
        return self.state in {IncidentState.RESOLVED.value, IncidentState.CLOSED.value, IncidentState.LEARNED.value}

    @property
    def active_hypotheses(self) -> list[CauseHypothesis]:
        return [h for h in self.hypotheses if not h.rejected]

    @property
    def ranked_hypotheses(self) -> list[CauseHypothesis]:
        # The agent's own ordering wins (it encodes confidence + cause priority).
        return sorted(self.active_hypotheses, key=lambda h: (h.rank, -h.confidence))

    @property
    def top_hypothesis(self) -> CauseHypothesis | None:
        ranked = self.ranked_hypotheses
        return ranked[0] if ranked else None

    @property
    def confirmed_hypothesis(self) -> CauseHypothesis | None:
        for h in self.hypotheses:
            if h.confirmed:
                return h
        return None

    def executed_actions(self) -> list[ActionSpec]:
        return [a for a in self.actions if a.result is not None]

    def failed_actions(self) -> list[ActionSpec]:
        return [
            a
            for a in self.executed_actions()
            if a.result is not None
            and (a.result.helped is False or a.result.outcome in {Outcome.TEMPORARY, Outcome.HURT, Outcome.NO_EFFECT})
        ]

    def successful_actions(self) -> list[ActionSpec]:
        return [a for a in self.executed_actions() if a.result is not None and a.result.helped is True]

    def blocked_actions(self) -> list[ActionSpec]:
        return [a for a in self.actions if a.status == ActionStatus.BLOCKED_BY_MEMORY or a.blocked_reason]

    def diagnostics(self) -> list[ActionSpec]:
        return [a for a in self.executed_actions() if a.risk == "READ_ONLY"]

    def remediation_attempts(self) -> list[ActionSpec]:
        return [a for a in self.executed_actions() if a.risk != "READ_ONLY"]

    def evidence_of(self, *kinds: EvidenceKind) -> list[EvidenceRef]:
        return [e for e in self.evidence if e.kind in kinds]

    def metric_value(self, name: str) -> float | None:
        if name in self.sim_metrics:
            return float(self.sim_metrics[name])
        for m in self.metrics:
            if m.get("name") == name:
                try:
                    return float(m.get("value"))
                except (TypeError, ValueError):
                    return None
        return None

    def resolution_minutes(self) -> float | None:
        if not self.resolved_at:
            return None
        start = _aware(self.detected_at) or _aware(self.started_at)
        end = _aware(self.resolved_at)
        if not start or not end:
            return None
        return round((end - start).total_seconds() / 60.0, 2)

    def timeline(self) -> list[TimelineEvent]:
        return sorted(self.events, key=lambda e: (e.seq, e.ts))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scenario_id": self.scenario_id,
            "service": self.service,
            "title": self.title,
            "severity": self.severity,
            "state": self.state,
            "symptom": self.symptom,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "detected_at": self.detected_at.isoformat() if self.detected_at else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "root_cause": self.root_cause,
            "root_cause_id": self.root_cause_id,
            "resolution": self.resolution,
            "step_count": self.step_count,
            "confirmed_step": self.confirmed_step,
            "memory_mode": self.memory_mode,
            "memory_assisted": self.memory_assisted,
            "memory_contribution": self.memory_contribution,
            "blocked_action_count": self.blocked_action_count,
            "stage_id": self.stage_id,
            "evidence": [e.model_dump(mode="json") for e in self.evidence],
            "hypotheses": [h.model_dump(mode="json") for h in self.ranked_hypotheses],
            "actions": [a.model_dump(mode="json") for a in self.actions],
            "events": [e.model_dump(mode="json") for e in self.timeline()],
        }


# --------------------------------------------------------------------------- ORM -> record


def record_from_orm(incident: orm.Incident, *, scenario: Scenario | None = None) -> IncidentRecord:
    factors = [SeverityFactor.model_validate(f) for f in (incident.severity_factors or []) if isinstance(f, dict)]
    severity_assessment = None
    if factors or incident.severity_explanation:
        from recallops.domain.enums import Severity

        try:
            sev = Severity(incident.severity)
        except ValueError:
            sev = Severity.SEV3
        severity_assessment = SeverityAssessment(
            severity=sev,
            score=incident.severity_score or 0.0,
            factors=factors,
            explanation=incident.severity_explanation or "",
        )
    impact = None
    if incident.impact:
        from recallops.domain.models import ImpactAssessment as _IA

        impact = _IA.model_validate(incident.impact)

    return IncidentRecord(
        id=incident.id,
        scenario_id=incident.scenario_id,
        service=incident.service,
        title=incident.title,
        severity=incident.severity,
        state=incident.state,
        symptom=incident.symptom or "",
        started_at=_aware(incident.started_at) or datetime.now(timezone.utc),
        detected_at=_aware(incident.detected_at) or datetime.now(timezone.utc),
        resolved_at=_aware(incident.resolved_at),
        root_cause=incident.root_cause or "",
        root_cause_id=incident.root_cause_id or "",
        resolution=incident.resolution or "",
        step_count=incident.step_count or 0,
        confirmed_step=incident.confirmed_step or 0,
        memory_mode=incident.memory_mode or "",
        memory_enabled=bool(incident.memory_enabled),
        memory_assisted=bool(incident.memory_assisted),
        memory_contribution=incident.memory_contribution or 0.0,
        top_hypothesis_confidence=incident.top_hypothesis_confidence or 0.0,
        top_hypothesis_cause_id=incident.top_hypothesis_cause_id or "",
        blocked_action_count=incident.blocked_action_count or 0,
        severity_assessment=severity_assessment,
        impact=impact,
        evidence=[evidence_from_orm(e) for e in incident.evidence],
        hypotheses=[hypothesis_from_orm(h) for h in incident.hypotheses],
        actions=[action_from_orm(a) for a in incident.actions],
        events=[event_from_orm(e) for e in incident.events],
        memories=[
            MemoryItem(
                id=m.id,
                kind=MemoryKind(m.kind),
                title=m.title,
                content=m.content,
                service=m.service,
                incident_id=m.incident_id or "",
                cause_id=m.cause_id,
                action_id=m.action_id,
                outcome=m.outcome,
                helped=(m.meta or {}).get("helped"),
                reusable_lesson=str((m.meta or {}).get("reusable_lesson") or ""),
                lesson=str((m.meta or {}).get("lesson") or ""),
                actual_outcome=str((m.meta or {}).get("actual_outcome") or ""),
                tags=list(m.tags or []),
                source=m.source,  # type: ignore[arg-type]
                occurred_at=_aware(m.created_at),
                durability=m.durability,  # type: ignore[arg-type]
            )
            for m in incident.memories
        ],
        deployments=[
            {
                "version": d.version,
                "deployed_at": _aware(d.deployed_at).isoformat() if _aware(d.deployed_at) else None,
                "minutes_before_incident": d.minutes_before_incident,
                "change_summary": d.change_summary,
                "author": d.author,
                "status": d.status,
                "risk": d.risk,
                "correlation": d.correlation,
                "tags": list(d.meta.get("tags", []) if d.meta else []),
            }
            for d in incident.deployments
        ],
        metrics=[
            {
                "name": m.name,
                "label": m.name,
                "value": m.value,
                "unit": m.unit,
                "baseline": m.baseline,
                "limit": m.limit_value,
                "ts": _aware(m.ts).isoformat() if _aware(m.ts) else None,
                "stage": m.stage,
            }
            for m in incident.metrics
        ],
        logs=[
            {
                "ts": _aware(l.ts).isoformat() if _aware(l.ts) else None,
                "level": l.level,
                "logger": l.logger,
                "message": l.message,
                "count": l.count,
                "stage": l.stage,
            }
            for l in incident.logs
        ],
        dependencies=[
            {
                "name": d.name,
                "kind": d.kind,
                "direction": d.direction,
                "status": d.status,
                "criticality": d.criticality,
                "latency_p95_ms": d.latency_p95_ms,
                "error_rate_pct": d.error_rate_pct,
            }
            for d in incident.dependencies
        ],
        feedback=[
            {
                "id": f.id,
                "target_type": f.target_type,
                "target_id": f.target_id,
                "verdict": f.verdict,
                "comment": f.comment,
                "corrected_cause": f.corrected_cause,
                "author": f.author,
                "created_at": _aware(f.created_at).isoformat() if _aware(f.created_at) else None,
            }
            for f in incident.feedback
        ],
        stage_id=str((incident.meta or {}).get("stage_id", "")),
        sim_metrics={k: float(v) for k, v in ((incident.meta or {}).get("sim_metrics", {}) or {}).items()},
        recalled_memory_ids=list((incident.meta or {}).get("recalled_memory_ids", []) or []),
        scenario=scenario,
        conflicts=list((incident.meta or {}).get("conflicts", []) or []),
    )


__all__ = [
    "IncidentRecord",
    "action_from_orm",
    "evidence_from_orm",
    "event_from_orm",
    "hypothesis_from_orm",
    "record_from_orm",
]
