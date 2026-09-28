"""Pydantic DTOs shared by the agent, services and API layers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from recallops.domain.enums import (
    ActionStatus,
    ActionType,
    EvidenceKind,
    MemoryKind,
    MemoryMode,
    Outcome,
    RiskLevel,
    Severity,
    TimelinePhase,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --------------------------------------------------------------------------- evidence
class EvidenceRef(ORMModel):
    """A single normalized piece of evidence with full provenance."""

    id: str
    kind: EvidenceKind
    source: str
    title: str
    detail: str = ""
    ts: datetime | None = None
    stage: str | None = None
    signal_hints: list[str] = Field(default_factory=list)
    raw: dict[str, Any] = Field(default_factory=dict)
    untrusted: bool = True
    redacted: bool = False


# --------------------------------------------------------------------------- memory
class MemoryLink(ORMModel):
    """A recalled memory plus *why* it was surfaced."""

    id: str
    kind: MemoryKind
    text: str
    source: MemoryMode
    score: float = 0.0
    incident_id: str | None = None
    service: str | None = None
    relevance: Literal["relevant", "weakly_relevant", "irrelevant"] = "relevant"
    why: str = ""
    strategy_hits: list[str] = Field(default_factory=list)
    created_at: datetime | None = None


class MemoryConflict(ORMModel):
    """Historical memory disagreeing with current incident evidence."""

    id: str
    subject: str
    historical_claim: str
    memory_ref: str | None = None
    current_evidence: list[str] = Field(default_factory=list)
    verdict: str
    resolution: str
    severity: Literal["info", "warning", "critical"] = "warning"


# --------------------------------------------------------------------------- hypotheses
class CauseHypothesis(ORMModel):
    id: str
    cause: str
    category: str
    confidence: float
    confidence_basis: str
    supporting: list[EvidenceRef] = Field(default_factory=list)
    contradicting: list[EvidenceRef] = Field(default_factory=list)
    precedent: list[str] = Field(default_factory=list)
    memory_links: list[MemoryLink] = Field(default_factory=list)
    memory_contribution: float = 0.0
    next_diagnostic: str
    rationale: str
    signals: list[str] = Field(default_factory=list)
    confirmed: bool = False
    rejected: bool = False
    rejected_reason: str | None = None
    origin: Literal["rule_engine", "llm", "engineer"] = "rule_engine"
    rank: int = 0

    @field_validator("confidence")
    @classmethod
    def _clamp(cls, v: float) -> float:
        return max(0.0, min(0.99, round(float(v), 3)))

    @property
    def cause_id(self) -> str:
        """The cause family id this hypothesis is about."""
        return self.id.split("::", 1)[-1]


# --------------------------------------------------------------------------- actions
class WhatIfResult(ORMModel):
    """Predicted effect of an action, produced by the deterministic simulator."""

    projection: list[dict[str, Any]] = Field(default_factory=list)
    predicted_outcome: Outcome = Outcome.NO_EFFECT
    predicted_detail: str = ""
    will_resolve: bool = False
    expected_signal: str = ""
    confidence: float = 0.5
    notes: list[str] = Field(default_factory=list)


class ActionResult(ORMModel):
    outcome: Outcome = Outcome.NO_EFFECT
    verdict: str = ""
    detail: str = ""
    observed: dict[str, Any] = Field(default_factory=dict)
    helped: bool | None = None
    reverted: bool = False
    lesson: str | None = None
    executed_at: datetime | None = None


class ActionSpec(ORMModel):
    id: str
    incident_id: str
    description: str
    type: ActionType = ActionType.DIAGNOSTIC
    risk: RiskLevel = RiskLevel.READ_ONLY
    reason: str
    expected_signal: str
    requires_confirmation: bool = False
    status: ActionStatus = ActionStatus.PROPOSED
    tool: str = ""
    params: dict[str, Any] = Field(default_factory=dict)
    reversible: bool = True
    production_impact: bool = False
    data_loss_risk: bool = False
    blocked_reason: str | None = None
    what_if: WhatIfResult | None = None
    result: ActionResult | None = None
    hypothesis_id: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    decided_at: datetime | None = None
    step_index: int = 0
    memory_warnings: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)


class Recommendation(ORMModel):
    action: ActionSpec
    why: str
    expected_signal: str
    risk: RiskLevel
    requires_approval: bool
    warnings: list[str] = Field(default_factory=list)
    alternatives: list[ActionSpec] = Field(default_factory=list)
    blocked_by_memory: bool = False


# --------------------------------------------------------------------------- severity / impact
class SeverityFactor(ORMModel):
    name: str
    value: str
    weight: float
    detail: str = ""


class SeverityAssessment(ORMModel):
    severity: Severity
    score: float
    confidence: float = 0.8
    factors: list[SeverityFactor] = Field(default_factory=list)
    explanation: str = ""
    auto_detected: bool = False


class ImpactAssessment(ORMModel):
    error_rate_pct: float | None = None
    baseline_error_rate_pct: float | None = None
    affected_endpoints: list[str] = Field(default_factory=list)
    customer_impact: Literal["none", "low", "moderate", "high", "severe"] = "low"
    service_criticality: Literal["standard", "important", "critical"] = "standard"
    dependency_impact: list[str] = Field(default_factory=list)
    estimated_affected_requests: int | None = None


# --------------------------------------------------------------------------- timeline
class TimelineEvent(ORMModel):
    id: str
    incident_id: str
    seq: int = 0
    ts: datetime = Field(default_factory=utcnow)
    phase: TimelinePhase
    title: str
    detail: str = ""
    actor: str = "system"
    meta: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- LLM structured output
class AnalystVote(ORMModel):
    cause: str
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    supporting: list[str] = Field(default_factory=list)
    contradicting: list[str] = Field(default_factory=list)
    next_diagnostic: str = ""


class Assumption(ORMModel):
    statement: str
    verified: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class AnalystVerdict(ORMModel):
    summary: str = ""
    votes: list[AnalystVote] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    raw_text: str = ""


class GroundedAnalysis(ORMModel):
    """Result of the LLM reasoning pass, provenance-checked against evidence."""

    ok: bool = False
    provider: str = ""
    model: str = ""
    summary: str = ""
    votes: list[AnalystVote] = Field(default_factory=list)
    rejected_votes: list[dict[str, str]] = Field(default_factory=list)
    grounded_votes: list[AnalystVote] = Field(default_factory=list)
    error: str | None = None
    latency_ms: int = 0
    used_memory: bool = False


# --------------------------------------------------------------------------- postmortem / runbook
class PostmortemSection(ORMModel):
    heading: str
    body: str = ""
    bullets: list[str] = Field(default_factory=list)


class PostmortemReport(ORMModel):
    incident_id: str
    title: str
    severity: Severity
    service: str
    summary: str
    impact: str
    timeline: list[TimelineEvent] = Field(default_factory=list)
    root_cause: str
    contributing_factors: list[str] = Field(default_factory=list)
    evidence: list[EvidenceRef] = Field(default_factory=list)
    actions_taken: list[ActionSpec] = Field(default_factory=list)
    failed_actions: list[ActionSpec] = Field(default_factory=list)
    successful_actions: list[ActionSpec] = Field(default_factory=list)
    resolution: str
    prevention: list[str] = Field(default_factory=list)
    runbook_changes: list[str] = Field(default_factory=list)
    lessons: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)
    generated_by: str = "recallops-agent"
    memory_written: bool = False
    memory_written_count: int = 0


class RunbookStep(ORMModel):
    order: int
    step: str
    kind: Literal["check", "diagnose", "act", "verify", "avoid"] = "check"
    detail: str = ""


class Runbook(ORMModel):
    id: str
    title: str
    service: str
    symptoms: list[str] = Field(default_factory=list)
    first_checks: list[str] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    known_failed_actions: list[str] = Field(default_factory=list)
    recommended_actions: list[str] = Field(default_factory=list)
    verification: list[str] = Field(default_factory=list)
    rollback: list[str] = Field(default_factory=list)
    prevention: list[str] = Field(default_factory=list)
    steps: list[RunbookStep] = Field(default_factory=list)
    source_incidents: list[str] = Field(default_factory=list)
    generated_at: datetime = Field(default_factory=utcnow)


__all__ = [n for n in dir() if n[0].isupper()] + ["utcnow"]
