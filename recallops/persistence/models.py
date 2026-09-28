"""SQLAlchemy models: application state, evidence, hypotheses, actions, memory mirror.

Hindsight is the durable organizational memory; this database is the working
state of the incident response system plus an auditable mirror of what was
retained (``MemoryRecord``) so a reviewer can inspect provenance without a
Hindsight account.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from recallops.persistence.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Incident(Base):
    __tablename__ = "incidents"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64), index=True)
    service: Mapped[str] = mapped_column(String(128), index=True)
    title: Mapped[str] = mapped_column(String(255))
    severity: Mapped[str] = mapped_column(String(16), default="SEV-3")
    state: Mapped[str] = mapped_column(String(32), default="NEW", index=True)
    symptom: Mapped[str] = mapped_column(Text, default="")
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    severity_score: Mapped[float] = mapped_column(Float, default=0.0)
    severity_explanation: Mapped[str] = mapped_column(Text, default="")
    severity_factors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    impact: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    root_cause: Mapped[str] = mapped_column(String(255), default="")
    root_cause_id: Mapped[str] = mapped_column(String(128), default="")
    resolution: Mapped[str] = mapped_column(Text, default="")
    resolution_summary: Mapped[str] = mapped_column(Text, default="")
    memory_mode: Mapped[str] = mapped_column(String(32), default="")
    memory_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    step_count: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_step: Mapped[int] = mapped_column(Integer, default=0)
    memory_assisted: Mapped[bool] = mapped_column(Boolean, default=False)
    memory_contribution: Mapped[float] = mapped_column(Float, default=0.0)
    memory_warning_triggered: Mapped[bool] = mapped_column(Boolean, default=False)
    top_hypothesis_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    top_hypothesis_cause_id: Mapped[str] = mapped_column(String(128), default="")
    blocked_action_count: Mapped[int] = mapped_column(Integer, default=0)
    auto_replay: Mapped[bool] = mapped_column(Boolean, default=False)
    is_demo: Mapped[bool] = mapped_column(Boolean, default=True)
    run_id: Mapped[str] = mapped_column(String(64), default="", index=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    evidence: Mapped[list["EvidenceEvent"]] = relationship(back_populates="incident", cascade="all, delete-orphan", order_by="EvidenceEvent.seq")
    hypotheses: Mapped[list["Hypothesis"]] = relationship(back_populates="incident", cascade="all, delete-orphan", order_by="Hypothesis.rank")
    actions: Mapped[list["ActionAttempt"]] = relationship(back_populates="incident", cascade="all, delete-orphan", order_by="ActionAttempt.step_index")
    events: Mapped[list["IncidentEvent"]] = relationship(back_populates="incident", cascade="all, delete-orphan", order_by="IncidentEvent.seq")
    feedback: Mapped[list["EngineerFeedback"]] = relationship(back_populates="incident", cascade="all, delete-orphan")
    memories: Mapped[list["MemoryRecord"]] = relationship(back_populates="incident", cascade="all, delete-orphan")
    postmortem: Mapped["Postmortem | None"] = relationship(back_populates="incident", cascade="all, delete-orphan", uselist=False)
    deployments: Mapped[list["Deployment"]] = relationship(back_populates="incident", cascade="all, delete-orphan")
    metrics: Mapped[list["MetricSample"]] = relationship(back_populates="incident", cascade="all, delete-orphan")
    logs: Mapped[list["LogEvent"]] = relationship(back_populates="incident", cascade="all, delete-orphan")
    dependencies: Mapped[list["ServiceDependency"]] = relationship(back_populates="incident", cascade="all, delete-orphan")


class EvidenceEvent(Base):
    __tablename__ = "evidence_events"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    kind: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(128), default="simulator")
    title: Mapped[str] = mapped_column(String(512), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    stage: Mapped[str] = mapped_column(String(64), default="", index=True)
    signal_hints: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    untrusted: Mapped[bool] = mapped_column(Boolean, default=True)
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)

    incident: Mapped[Incident] = relationship(back_populates="evidence")


class Hypothesis(Base):
    __tablename__ = "hypotheses"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    rank: Mapped[int] = mapped_column(Integer, default=0)
    cause_id: Mapped[str] = mapped_column(String(128), default="")
    cause: Mapped[str] = mapped_column(String(255), default="")
    category: Mapped[str] = mapped_column(String(64), default="")
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_basis: Mapped[str] = mapped_column(Text, default="")
    rationale: Mapped[str] = mapped_column(Text, default="")
    next_diagnostic: Mapped[str] = mapped_column(Text, default="")
    supporting: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    contradicting: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    precedent: Mapped[list[str]] = mapped_column(JSON, default=list)
    memory_links: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    memory_contribution: Mapped[float] = mapped_column(Float, default=0.0)
    signals: Mapped[list[str]] = mapped_column(JSON, default=list)
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    rejected: Mapped[bool] = mapped_column(Boolean, default=False)
    rejected_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    origin: Mapped[str] = mapped_column(String(32), default="rule_engine")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    incident: Mapped[Incident] = relationship(back_populates="hypotheses")


class ActionAttempt(Base):
    __tablename__ = "action_attempts"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    step_index: Mapped[int] = mapped_column(Integer, default=0)
    description: Mapped[str] = mapped_column(Text, default="")
    type: Mapped[str] = mapped_column(String(32), default="diagnostic")
    risk: Mapped[str] = mapped_column(String(32), default="READ_ONLY")
    status: Mapped[str] = mapped_column(String(32), default="proposed", index=True)
    reason: Mapped[str] = mapped_column(Text, default="")
    expected_signal: Mapped[str] = mapped_column(Text, default="")
    requires_confirmation: Mapped[bool] = mapped_column(Boolean, default=False)
    tool: Mapped[str] = mapped_column(String(64), default="")
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reversible: Mapped[bool] = mapped_column(Boolean, default=True)
    production_impact: Mapped[bool] = mapped_column(Boolean, default=False)
    data_loss_risk: Mapped[bool] = mapped_column(Boolean, default=False)
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    memory_warnings: Mapped[list[str]] = mapped_column(JSON, default=list)
    safety_notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    hypothesis_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    what_if: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    predicted_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    predicted_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    incident: Mapped[Incident] = relationship(back_populates="actions")


class EngineerFeedback(Base):
    __tablename__ = "engineer_feedback"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    target_type: Mapped[str] = mapped_column(String(32), default="hypothesis")  # hypothesis | action
    target_id: Mapped[str] = mapped_column(String(96), default="")
    verdict: Mapped[str] = mapped_column(String(32), default="")  # correct | incorrect | helpful | not_helpful
    comment: Mapped[str] = mapped_column(Text, default="")
    corrected_cause: Mapped[str] = mapped_column(String(255), default="")
    author: Mapped[str] = mapped_column(String(128), default="oncall")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    memory_record_id: Mapped[str | None] = mapped_column(String(96), nullable=True)

    incident: Mapped[Incident] = relationship(back_populates="feedback")


class Postmortem(Base):
    __tablename__ = "postmortems"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    severity: Mapped[str] = mapped_column(String(16), default="SEV-3")
    service: Mapped[str] = mapped_column(String(128), default="")
    summary: Mapped[str] = mapped_column(Text, default="")
    impact: Mapped[str] = mapped_column(Text, default="")
    root_cause: Mapped[str] = mapped_column(Text, default="")
    contributing_factors: Mapped[list[str]] = mapped_column(JSON, default=list)
    actions_taken: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    failed_actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    successful_actions: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    resolution: Mapped[str] = mapped_column(Text, default="")
    prevention: Mapped[list[str]] = mapped_column(JSON, default=list)
    runbook_changes: Mapped[list[str]] = mapped_column(JSON, default=list)
    lessons: Mapped[list[str]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    timeline: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    memory_written: Mapped[bool] = mapped_column(Boolean, default=False)
    memory_written_count: Mapped[int] = mapped_column(Integer, default=0)
    memory_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    incident: Mapped[Incident] = relationship(back_populates="postmortem")


class MemoryRecord(Base):
    """Auditable mirror of what the memory layer retained."""

    __tablename__ = "memory_records"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    incident_id: Mapped[str | None] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(48), index=True)
    durability: Mapped[str] = mapped_column(String(24), default="durable")
    title: Mapped[str] = mapped_column(String(255), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    service: Mapped[str] = mapped_column(String(128), default="", index=True)
    cause_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    action_id: Mapped[str | None] = mapped_column(String(96), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(32), default="local_hindsight")
    external_id: Mapped[str | None] = mapped_column(String(160), nullable=True)
    memory_type: Mapped[str] = mapped_column(String(32), default="world")
    entities: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)

    incident: Mapped[Incident | None] = relationship(back_populates="memories")


class Deployment(Base):
    __tablename__ = "deployments"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    service: Mapped[str] = mapped_column(String(128), default="")
    version: Mapped[str] = mapped_column(String(64), default="")
    deployed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    minutes_before_incident: Mapped[float | None] = mapped_column(Float, nullable=True)
    change_summary: Mapped[str] = mapped_column(Text, default="")
    author: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="healthy")
    risk: Mapped[str] = mapped_column(String(32), default="low")
    correlation: Mapped[float] = mapped_column(Float, default=0.0)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="deployments")


class MetricSample(Base):
    __tablename__ = "metric_samples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    name: Mapped[str] = mapped_column(String(128), index=True)
    value: Mapped[float] = mapped_column(Float, default=0.0)
    unit: Mapped[str] = mapped_column(String(32), default="")
    limit_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    baseline: Mapped[float | None] = mapped_column(Float, nullable=True)
    stage: Mapped[str] = mapped_column(String(64), default="")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="metrics")


class LogEvent(Base):
    __tablename__ = "log_events"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    level: Mapped[str] = mapped_column(String(16), default="INFO")
    logger: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(Text, default="")
    count: Mapped[int] = mapped_column(Integer, default=1)
    stage: Mapped[str] = mapped_column(String(64), default="")
    redacted: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="logs")


class ServiceDependency(Base):
    __tablename__ = "service_dependencies"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(32), default="internal")
    direction: Mapped[str] = mapped_column(String(16), default="downstream")
    status: Mapped[str] = mapped_column(String(32), default="healthy")
    criticality: Mapped[str] = mapped_column(String(32), default="standard")
    latency_p95_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_rate_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="dependencies")


class Runbook(Base):
    __tablename__ = "runbooks"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    service: Mapped[str] = mapped_column(String(128), default="", index=True)
    title: Mapped[str] = mapped_column(String(255), default="")
    cause_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    symptoms: Mapped[list[str]] = mapped_column(JSON, default=list)
    first_checks: Mapped[list[str]] = mapped_column(JSON, default=list)
    diagnostics: Mapped[list[str]] = mapped_column(JSON, default=list)
    known_failed_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    recommended_actions: Mapped[list[str]] = mapped_column(JSON, default=list)
    verification: Mapped[list[str]] = mapped_column(JSON, default=list)
    rollback: Mapped[list[str]] = mapped_column(JSON, default=list)
    prevention: Mapped[list[str]] = mapped_column(JSON, default=list)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source_incidents: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)


class IncidentEvent(Base):
    """Timeline / audit log entry (also the SSE event source)."""

    __tablename__ = "incident_events"

    id: Mapped[str] = mapped_column(String(96), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id", ondelete="CASCADE"), index=True)
    seq: Mapped[int] = mapped_column(Integer, default=0, index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, index=True)
    phase: Mapped[str] = mapped_column(String(32), default="system", index=True)
    title: Mapped[str] = mapped_column(String(512), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    actor: Mapped[str] = mapped_column(String(64), default="system")
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    incident: Mapped[Incident] = relationship(back_populates="events")


class SimulationRun(Base):
    """State of the live (scripted, deterministic) production simulator."""

    __tablename__ = "simulation_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scenario_id: Mapped[str] = mapped_column(String(64), default="INC-A1", index=True)
    status: Mapped[str] = mapped_column(String(24), default="running")  # running | paused | stopped | completed
    cursor: Mapped[int] = mapped_column(Integer, default=0)
    speed: Mapped[float] = mapped_column(Float, default=4.0)
    step_seconds: Mapped[float] = mapped_column(Float, default=1.0)
    auto_advance: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_analyze: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_resolve: Mapped[bool] = mapped_column(Boolean, default=False)
    incident_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)
    meta: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


Index("ix_evidence_incident_stage", EvidenceEvent.incident_id, EvidenceEvent.stage)
Index("ix_hypotheses_incident_rank", Hypothesis.incident_id, Hypothesis.rank)
Index("ix_actions_incident_step", ActionAttempt.incident_id, ActionAttempt.step_index)
Index("ix_memory_cause_service", MemoryRecord.cause_id, MemoryRecord.service)


__all__ = [
    "ActionAttempt",
    "Deployment",
    "EngineerFeedback",
    "EvidenceEvent",
    "Incident",
    "IncidentEvent",
    "LogEvent",
    "MemoryRecord",
    "MetricSample",
    "Postmortem",
    "Runbook",
    "ServiceDependency",
    "SimulationRun",
]
