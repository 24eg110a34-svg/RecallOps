"""The incident orchestrator.

Owns the loop that every other module serves:

    incident -> evidence -> memory recall -> hypotheses -> ranked root cause
             -> planned action -> safety gate -> (approval) -> simulated outcome
             -> postmortem -> durable memory -> the next incident is better

It is the only component that writes incident state, so the state machine, the
audit trail and the timeline can never drift apart.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any, Sequence

from sqlalchemy.orm import Session, sessionmaker

from recallops.agent.catalog import ActionDef, get_action
from recallops.agent.hypothesis import FeedbackHint, HypothesisEngine
from recallops.agent.evidence import (
    normalize_alert,
    normalize_deployment,
    normalize_dependency,
    normalize_log,
    normalize_metric,
)
from recallops.agent.action_planner import (
    ACTION_ID_SEP,
    ActionPlanner,
    definition_id_from_spec,
    failed_actions_from_memory,
)
from recallops.agent.memory_composer import compose_correction, compose_from_incident
from recallops.agent.postmortem import build_postmortem, build_runbook
from recallops.agent.query import build_memory_query
from recallops.agent.rca import EvidenceBundle, detect_signals, rule_based_votes
from recallops.agent.record import IncidentRecord, action_from_orm, record_from_orm
from recallops.agent.safety import SafetyGate, SafetyViolation
from recallops.agent.scenarios import Scenario, ScenarioError, ScenarioLibrary, get_library
from recallops.agent.simulator import ScenarioSimulator
from recallops.config import Settings, get_settings
from recallops.domain.enums import (
    ActionStatus,
    IncidentState,
    MemoryKind,
    Outcome,
    TimelinePhase,
    assert_transition,
    can_transition,
)
from recallops.domain.models import ActionSpec, CauseHypothesis, EvidenceRef
from recallops.domain.scoring import classify_severity, normalize_impact
from recallops.domain.signals import cause_name
from recallops.memory.models import MemoryRecallResult
from recallops.memory.port import MemoryPort
from recallops.persistence import models as orm
from recallops.security import redact_text
from recallops.services.events import publish_event, record_event
from recallops.services.llm.base import LLMProvider
from recallops.services.llm.factory import get_llm_provider


class IncidentNotFound(LookupError):
    pass


class ActionNotFound(LookupError):
    pass


class InvalidTransition(RuntimeError):
    pass


class IncidentOrchestrator:
    def __init__(
        self,
        *,
        session_factory: Any,
        settings: Settings | None = None,
        scenarios: ScenarioLibrary | None = None,
        memory: MemoryPort | None = None,
        llm: LLMProvider | None = None,
        safety: SafetyGate | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.settings = settings or get_settings()
        self.scenarios = scenarios or get_library()
        self.memory = memory
        self.llm = llm if llm is not None else get_llm_provider()
        self.safety = safety or SafetyGate()
        self.engine = HypothesisEngine(llm=self.llm)
        self.planner = ActionPlanner(self.safety)

    # ------------------------------------------------------------------ sessions
    def session(self) -> Session:
        """Open a session.

        Accepts either a plain factory (``lambda: Session()``) or
        ``persistence.get_sessionmaker`` itself, so wiring code never has to
        care which one it got.
        """
        candidate = self.session_factory()
        return candidate() if isinstance(candidate, sessionmaker) else candidate

    def get_incident(self, session: Session, incident_id: str) -> orm.Incident:
        incident = session.get(orm.Incident, incident_id)
        if incident is None:
            raise IncidentNotFound(f"unknown incident '{incident_id}'")
        return incident

    def scenario_for(self, incident: orm.Incident) -> Scenario | None:
        try:
            return self.scenarios.get(incident.scenario_id)
        except ScenarioError:
            return None

    def load_record(self, incident_id: str) -> IncidentRecord | None:
        session = self.session()
        try:
            incident = session.get(orm.Incident, incident_id)
            if incident is None:
                return None
            return record_from_orm(incident, scenario=self.scenario_for(incident))
        finally:
            session.close()

    # ------------------------------------------------------------------ creation
    def create_incident(
        self,
        scenario_id: str,
        *,
        incident_id: str | None = None,
        run_id: str = "",
        memory_enabled: bool = True,
        actor: str = "detector",
    ) -> dict[str, Any]:
        scenario = self.scenarios.get(scenario_id)
        incident_id = incident_id or scenario.id
        session = self.session()
        try:
            existing = session.get(orm.Incident, incident_id)
            if existing is not None:
                # Idempotent: return the existing incident rather than
                # duplicating evidence rows that would collide on primary key.
                return {
                    "incident": record_from_orm(existing, scenario=scenario).to_dict(),
                    "created": False,
                    "note": f"incident {incident_id} already exists; POST /api/demo/reset to replay it",
                }

            sim = ScenarioSimulator(scenario)
            started = scenario.base_time
            alert = normalize_alert(scenario.alert, incident_id, scenario.timestamp(0))
            severity, impact = self._assess(scenario, sim)

            incident = orm.Incident(
                id=incident_id,
                scenario_id=scenario.id,
                service=scenario.service,
                title=scenario.meta.title,
                severity=severity.severity.value,
                state=IncidentState.NEW.value,
                symptom=scenario.meta.symptom,
                detected_at=scenario.timestamp(0),
                started_at=started,
                severity_score=severity.score,
                severity_explanation=severity.explanation,
                severity_factors=[f.model_dump(mode="json") for f in severity.factors],
                impact=impact.model_dump(mode="json"),
                memory_enabled=memory_enabled,
                run_id=run_id or uuid.uuid4().hex[:12],
                is_demo=True,
                meta={"stage_id": sim.stage_id, "offset_s": sim.offset_s, "sim_metrics": sim.metrics, "scenario_path": str(scenario.path or "")},
            )
            session.add(incident)
            session.flush()

            self._persist_evidence(session, incident, [alert])
            self._persist_stage_data(session, incident, scenario, sim, include_logs=True)
            self._persist_context_rows(session, incident, scenario)
            self._persist_sim_state(incident, sim)

            event = record_event(
                session,
                incident_id=incident.id,
                phase=TimelinePhase.INCIDENT,
                title=f"{severity.severity.value} incident opened from {scenario.alert.alert_id}",
                detail=scenario.meta.symptom,
                actor=actor,
                meta={
                    "service": scenario.service,
                    "severity": severity.severity.value,
                    "severity_score": severity.score,
                    "auto_detected": True,
                },
                ts=scenario.timestamp(0),
            )
            if severity.auto_detected:
                record_event(
                    session,
                    incident_id=incident.id,
                    phase=TimelinePhase.EVIDENCE,
                    title=f"Severity classified as {severity.severity.value} (score {severity.score:.0f})",
                    detail=severity.explanation,
                    actor="severity-classifier",
                    meta={"factors": [f.model_dump(mode="json") for f in severity.factors]},
                    ts=scenario.timestamp(0),
                )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        publish_event(incident_id, event)
        return {"incident": self.load_record(incident_id).to_dict() if self.load_record(incident_id) else None, "created": True}

    def _assess(self, scenario: Scenario, sim: ScenarioSimulator):
        metrics = {m.name: m.value for m in scenario.initial_metrics()}
        error_rate = next(
            (v for name, v in metrics.items() if name in {"http_5xx_pct", "http_502_pct", "http_504_pct"} or name.endswith("_error_pct")),
            None,
        )
        degraded = [d.name for d in scenario.dependencies if d.status not in {"healthy", "ok", "up"} and d.name != scenario.service]
        endpoints = [d.name for d in scenario.dependencies if d.direction == "upstream"]
        severity = classify_severity(
            error_rate_pct=error_rate,
            baseline_error_rate_pct=scenario.meta.baseline_error_rate_pct,
            service_criticality=scenario.meta.service_criticality,
            customer_impact=scenario.meta.customer_impact,
            affected_endpoints=endpoints,
            dependency_impact=degraded,
            duration_min=0.0,
            latency_p95_ms=metrics.get("latency_p95_ms") or metrics.get("checkout_latency_p95_ms"),
            baseline_latency_ms=metrics.get("latency_p95_ms") and _baseline_latency(scenario),
            cpu_utilization_pct=metrics.get("cpu_utilization_pct"),
            memory_utilization_pct=metrics.get("memory_utilization_pct"),
            throttled_pct=metrics.get("throttled_requests_pct"),
            auto_detected=True,
        )
        impact = normalize_impact(
            error_rate_pct=error_rate,
            baseline_error_rate_pct=scenario.meta.baseline_error_rate_pct,
            affected_endpoints=endpoints,
            service_criticality=scenario.meta.service_criticality,
            dependency_impact=degraded,
            rps=metrics.get("rps"),
        )
        return severity, impact

    # ------------------------------------------------------------------ analysis
    async def analyze(
        self,
        incident_id: str,
        *,
        memory_enabled: bool = True,
        actor: str = "agent",
        reason: str = "",
    ) -> dict[str, Any]:
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            scenario = self.scenario_for(incident)
            if scenario is None:
                raise IncidentNotFound(f"scenario for {incident_id} is not available")
            sim = self._simulator(incident, scenario)
            record = record_from_orm(incident, scenario=scenario)
            record.stage_id = sim.stage_id
            record.sim_metrics = sim.metrics

            bundle = self._bundle(record, scenario, sim)

            if incident.state == IncidentState.NEW.value:
                self._transition(incident, IncidentState.TRIAGING)
                record_event(
                    session,
                    incident_id=incident.id,
                    phase=TimelinePhase.EVIDENCE,
                    title=f"Collected {len(bundle.evidence)} evidence items for triage",
                    detail="; ".join(sorted({e.kind.value for e in bundle.evidence})),
                    actor="analyzer",
                    meta={"evidence_count": len(bundle.evidence)},
                    ts=sim.timestamp(),
                )

            memory_result = await self._recall(bundle, memory_enabled=memory_enabled and incident.memory_enabled)
            bundle.memories = memory_result.items

            feedback = [
                FeedbackHint(
                    target_id=f.target_id,
                    verdict=f.verdict,
                    comment=f.comment,
                    corrected_cause=f.corrected_cause,
                )
                for f in incident.feedback
            ]
            outcome = await self.engine.run(bundle, memory_result, feedback=feedback)

            self._persist_hypotheses(session, incident, outcome.hypotheses, outcome.conflicts)
            self._persist_memory_mirror(session, incident, memory_result)

            top = outcome.top
            incident.memory_mode = memory_result.mode.value
            incident.memory_assisted = outcome.memory_assisted
            incident.memory_contribution = outcome.memory_contribution
            incident.top_hypothesis_confidence = top.confidence if top else 0.0
            incident.top_hypothesis_cause_id = top.cause_id if top else ""
            if memory_result.degraded:
                incident.meta = {**(incident.meta or {}), "memory_degraded_reason": memory_result.degraded_reason}

            plan = self.planner.plan(
                incident_id=incident.id,
                hypotheses=outcome.hypotheses,
                memories=memory_result.items,
                available_action_ids=sim.available_actions(),
                executed_action_ids=[definition_id_from_spec(a.id) for a in incident.actions if a.result is not None],
                state=IncidentState(incident.state),
                step_index=self._next_step_index(incident),
            )
            self._persist_plan(session, incident, plan)
            incident.blocked_action_count = len(plan.blocked)

            events: list[Any] = []
            events.append(
                record_event(
                    session,
                    incident_id=incident.id,
                    phase=TimelinePhase.MEMORY_RECALL,
                    title=(
                        f"Recalled {len(memory_result.items)} organisation memories via {memory_result.mode.value}"
                        if memory_result.items
                        else f"No relevant memory found ({memory_result.mode.value}) - investigating from current evidence only"
                    ),
                    detail=(
                        "; ".join((m.reusable_lesson or m.title)[:120] for m in memory_result.items[:3])
                        or "Memory layer returned nothing relevant. This is the cold-start case."
                    ),
                    actor="memory",
                    meta={
                        "mode": memory_result.mode.value,
                        "provider": memory_result.provider,
                        "total_found": memory_result.total_found,
                        "relevant": memory_result.relevant_count,
                        "strategies": memory_result.strategy_summary,
                        "degraded": memory_result.degraded,
                        "degraded_reason": memory_result.degraded_reason,
                        "memory_ids": [m.id for m in memory_result.items[:10]],
                    },
                    ts=sim.timestamp(),
                )
            )
            if top is not None:
                events.append(
                    record_event(
                        session,
                        incident_id=incident.id,
                        phase=TimelinePhase.HYPOTHESIS,
                        title=f"Leading hypothesis: {top.cause} ({top.confidence:.0%})",
                        detail=top.rationale,
                        actor="rca-engine",
                        meta={
                            "hypothesis_id": top.id,
                            "cause_id": top.cause_id,
                            "confidence": top.confidence,
                            "signals": top.signals,
                            "precedent": top.precedent,
                            "memory_contribution": top.memory_contribution,
                            "ranking": [
                                {"cause": h.cause, "cause_id": h.cause_id, "confidence": h.confidence} for h in outcome.hypotheses
                            ],
                        },
                        ts=sim.timestamp(),
                    )
                )
            failed_memories = failed_actions_from_memory(memory_result.items)
            if memory_result.items:
                # Visible even before the action becomes available to propose.
                memory_result.model_fields_set  # no-op: keeps pydantic import path explicit
            if failed_memories:
                incident.memory_warning_triggered = True
                for memory in failed_memories[:3]:
                    events.append(
                        record_event(
                            session,
                            incident_id=incident.id,
                            phase=TimelinePhase.MEMORY_RECALL,
                            title=f"LEARNED LESSON recalled: {memory.action or 'a previous action'} previously failed",
                            detail=memory.reusable_lesson or memory.lesson or memory.content[:300],
                            actor="memory",
                            meta={
                                "memory_id": memory.id,
                                "incident_id": memory.incident_id,
                                "helped": memory.helped,
                                "outcome": memory.outcome,
                                "kind": "failed_action_warning",
                            },
                            ts=sim.timestamp(),
                        )
                    )

            for conflict in outcome.conflicts:
                events.append(
                    record_event(
                        session,
                        incident_id=incident.id,
                        phase=TimelinePhase.MEMORY_RECALL,
                        title=f"Historical memory conflict: {conflict.subject}",
                        detail=conflict.resolution,
                        actor="contradiction-engine",
                        meta={"conflict": conflict.model_dump(mode="json")},
                        ts=sim.timestamp(),
                    )
                )
            if plan.recommendation is not None:
                rec = plan.recommendation
                events.append(
                    record_event(
                        session,
                        incident_id=incident.id,
                        phase=TimelinePhase.RECOMMENDATION,
                        title=f"Recommended: {rec.action.description} [{rec.risk.value}]",
                        detail=rec.why,
                        actor="action-planner",
                        meta={
                            "action_id": rec.action.id,
                            "risk": rec.risk.value,
                            "requires_approval": rec.requires_approval,
                            "expected_signal": rec.expected_signal,
                            "blocked_by_memory": rec.blocked_by_memory,
                            "warnings": rec.warnings,
                        },
                        ts=sim.timestamp(),
                    )
                )
            for blocked in plan.blocked:
                events.append(
                    record_event(
                        session,
                        incident_id=incident.id,
                        phase=TimelinePhase.RECOMMENDATION,
                        title=f"Blocked by memory: {blocked.description}",
                        detail=blocked.blocked_reason or "",
                        actor="action-planner",
                        meta={"action_id": blocked.id, "warnings": blocked.memory_warnings},
                        ts=sim.timestamp(),
                    )
                )

            if incident.state == IncidentState.TRIAGING.value:
                self._transition(incident, IncidentState.INVESTIGATING)
            if reason:
                record_event(
                    session,
                    incident_id=incident.id,
                    phase=TimelinePhase.SYSTEM,
                    title="Analysis requested",
                    detail=reason,
                    actor=actor,
                    ts=sim.timestamp(),
                )
            session.commit()
            payload = {
                "incident_id": incident.id,
                "state": incident.state,
                "stage": sim.stage_id,
                "memory": memory_result.model_dump(mode="json"),
                "hypotheses": [h.model_dump(mode="json") for h in outcome.hypotheses],
                "conflicts": [c.model_dump(mode="json") for c in outcome.conflicts],
                "recommendation": plan.recommendation.model_dump(mode="json") if plan.recommendation else None,
                "blocked_actions": [a.model_dump(mode="json") for a in plan.blocked],
                "notes": outcome.notes + plan.notes,
                "memory_warnings": [
                    {
                        "memory_id": m.id,
                        "incident_id": m.incident_id,
                        "action": m.action or m.action_id,
                        "lesson": m.reusable_lesson or m.lesson,
                        "outcome": m.outcome,
                    }
                    for m in failed_memories
                ],
                "deploy_correlation": {"score": outcome.deploy_correlation[0], "detail": outcome.deploy_correlation[1]},
                "llm": outcome.analysis.model_dump(mode="json") if outcome.analysis else None,
                "signals": {sid: {"label": h.label, "strength": h.strength, "evidence": [e.id for e in h.evidence]} for sid, h in outcome.signals.items()},
            }
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        for event in events:
            publish_event(incident_id, event)
        return payload

    # ------------------------------------------------------------------ memory
    async def _recall(self, bundle: EvidenceBundle, *, memory_enabled: bool = True) -> MemoryRecallResult:
        if not memory_enabled or self.memory is None:
            from recallops.domain.enums import MemoryMode
            from recallops.memory.models import MemoryRecallResult

            return MemoryRecallResult(
                items=[],
                mode=MemoryMode.DISABLED,
                provider="disabled",
                query=bundle.searchable_text()[:300],
                detail="memory disabled for this run",
            )
        query = build_memory_query(bundle, exclude_incident_id=bundle.incident_id)
        try:
            return self.memory.recall(query, scope=bundle.service)
        except Exception as exc:  # noqa: BLE001 - memory failure must not break analysis
            from recallops.domain.enums import MemoryMode
            from recallops.memory.models import MemoryRecallResult

            return MemoryRecallResult(
                items=[],
                mode=MemoryMode.DEMO_FALLBACK,
                provider="error",
                degraded=True,
                degraded_reason=f"Memory recall failed: {redact_text(str(exc))[:200]} - continuing with current evidence only.",
                error={"kind": "recall_failed", "message": redact_text(str(exc))[:200]},
                query=query.text[:300],
            )

    # ------------------------------------------------------------------ advance
    async def advance(self, incident_id: str, *, analyze: bool = True) -> dict[str, Any]:
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            scenario = self.scenario_for(incident)
            if scenario is None:
                raise IncidentNotFound(f"scenario for {incident_id} is missing")
            sim = self._simulator(incident, scenario)
            state, revealed, metrics = sim.advance()
            new_evidence: list[EvidenceRef] = []
            if revealed:
                new_evidence = self._persist_evidence(session, incident, revealed)
            if metrics:
                self._persist_metric_samples(session, incident, metrics, sim)
            self._persist_sim_state(incident, sim)
            event = record_event(
                session,
                incident_id=incident.id,
                phase=TimelinePhase.EVIDENCE,
                title=f"Simulation advanced to stage '{state.stage_label}'",
                detail=state.note or state.stage_label,
                actor="simulator",
                meta={"stage": state.stage_id, "metrics": state.metrics, "new_evidence": len(new_evidence)},
                ts=sim.timestamp(),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        publish_event(incident_id, event)
        result: dict[str, Any] = {"incident_id": incident_id, "stage": state.snapshot(), "new_evidence": len(new_evidence)}
        if analyze:
            result["analysis"] = await self.analyze(incident_id, reason=f"re-analysed after advancing to '{state.stage_id}'")
        return result

    # ------------------------------------------------------------------ actions
    def _next_step_index(self, incident: orm.Incident) -> int:
        return max((a.step_index or 0) for a in incident.actions) + 1 if incident.actions else 1

    def _persist_hypotheses(
        self,
        session: Session,
        incident: orm.Incident,
        hypotheses: Sequence[CauseHypothesis],
        conflicts: Sequence[Any] = (),
    ) -> None:
        existing = {h.id: h for h in incident.hypotheses}
        seen: set[str] = set()
        for rank, hyp in enumerate(hypotheses):
            seen.add(hyp.id)
            row = existing.get(hyp.id)
            if row is None:
                row = orm.Hypothesis(id=hyp.id, incident_id=incident.id)
                session.add(row)
            row.rank = rank
            row.cause_id = hyp.cause_id
            row.cause = hyp.cause
            row.category = hyp.category
            row.confidence = hyp.confidence
            row.confidence_basis = hyp.confidence_basis
            row.rationale = hyp.rationale
            row.next_diagnostic = hyp.next_diagnostic
            row.supporting = [e.model_dump(mode="json") for e in hyp.supporting]
            row.contradicting = [e.model_dump(mode="json") for e in hyp.contradicting]
            row.precedent = hyp.precedent
            row.memory_links = [m.model_dump(mode="json") for m in hyp.memory_links]
            row.memory_contribution = hyp.memory_contribution
            row.signals = hyp.signals
            row.confirmed = row.confirmed or hyp.confirmed
            row.rejected = hyp.rejected
            row.rejected_reason = hyp.rejected_reason
            row.origin = hyp.origin
        for hyp_id, row in existing.items():
            if hyp_id not in seen and not row.confirmed:
                session.delete(row)
        if conflicts:
            incident.meta = {**(incident.meta or {}), "conflicts": [c.model_dump(mode="json") for c in conflicts]}

    def _persist_memory_mirror(self, session: Session, incident: orm.Incident, result: MemoryRecallResult) -> None:
        """Record which memories were cited, for provenance and analytics."""
        if not result.items:
            return
        incident.meta = {
            **(incident.meta or {}),
            "recalled_memory_ids": [m.id for m in result.items],
            "recalled_memory_mode": result.mode.value,
            "memory_strategies": result.strategy_summary,
        }

    def _persist_plan(self, session: Session, incident: orm.Incident, plan: Any) -> None:
        known = {a.id: a for a in incident.actions}
        live_statuses = {
            ActionStatus.PROPOSED,
            ActionStatus.AWAITING_APPROVAL,
            ActionStatus.APPROVED,
            ActionStatus.BLOCKED_BY_MEMORY,
        }

        def upsert(spec: ActionSpec) -> None:
            row = known.get(spec.id)
            if row is None:
                row = orm.ActionAttempt(id=spec.id, incident_id=incident.id)
                session.add(row)
                known[spec.id] = row
            row.step_index = spec.step_index
            row.description = spec.description
            row.type = spec.type.value
            row.risk = spec.risk.value
            row.reason = spec.reason
            row.expected_signal = spec.expected_signal
            row.requires_confirmation = spec.requires_confirmation
            row.status = spec.status.value
            row.tool = spec.tool
            row.params = spec.params
            row.reversible = spec.reversible
            row.production_impact = spec.production_impact
            row.data_loss_risk = spec.data_loss_risk
            row.blocked_reason = spec.blocked_reason
            row.memory_warnings = spec.memory_warnings
            row.safety_notes = spec.safety_notes
            row.hypothesis_id = spec.hypothesis_id
            if spec.what_if is not None and row.what_if is None:
                row.what_if = spec.what_if.model_dump(mode="json")

        specs: list[ActionSpec] = []
        if plan.recommendation is not None:
            specs.append(plan.recommendation.action)
            specs.extend(plan.recommendation.alternatives)
        specs.extend(plan.alternatives)
        specs.extend(plan.blocked)
        for spec in specs:
            upsert(spec)

        # Drop stale proposals that are no longer offered (they were superseded).
        current_ids = {s.id for s in specs}
        for action_id, row in list(known.items()):
            if action_id not in current_ids and row.status in {ActionStatus.PROPOSED.value, ActionStatus.AWAITING_APPROVAL.value} and not row.result:
                session.delete(row)
                known.pop(action_id, None)

    # ------------------------------------------------------------------ approve/reject
    def approve(self, incident_id: str, action_id: str, *, approved_by: str, note: str = "") -> dict[str, Any]:
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            row = self._get_action(session, incident, action_id)
            definition = self._definition(row)
            verdict = self.safety.authorize_execution(
                definition,
                state=IncidentState(incident.state),
                approved=True,
                approved_by=approved_by,
            )
            if verdict.decision.value == "refuse" and not self.safety.allow_high_risk_execution:
                event = record_event(
                    session,
                    incident_id=incident_id,
                    phase=TimelinePhase.APPROVAL,
                    title=f"Approval recorded but execution refused: {row.description}",
                    detail="; ".join(verdict.warnings) or "high-risk actions are advisory only",
                    actor=approved_by,
                    meta={"action_id": action_id, "verdict": verdict.to_dict()},
                )
                row.status = ActionStatus.BLOCKED_BY_MEMORY.value if row.blocked_reason else row.status
                row.decided_by = approved_by
                row.decision_reason = note
                row.decided_at = datetime.now(timezone.utc)
                session.commit()
                publish_event(incident_id, event)
                return {
                    "action_id": action_id,
                    "approved": True,
                    "executable": False,
                    "refused": True,
                    "verdict": verdict.to_dict(),
                    "message": "Approval recorded for the audit trail. RecallOps does not execute high-risk actions.",
                }
            row.status = ActionStatus.APPROVED.value
            row.decided_by = approved_by
            row.decision_reason = note
            row.decided_at = datetime.now(timezone.utc)
            event = record_event(
                session,
                incident_id=incident_id,
                phase=TimelinePhase.APPROVAL,
                title=f"Approved by {approved_by}: {row.description}",
                detail=(verdict.notes[0] if verdict.notes else note),
                actor=approved_by,
                meta={"action_id": action_id, "risk": row.risk, "verdict": verdict.to_dict()},
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        publish_event(incident_id, event)
        return {"action_id": action_id, "approved": True, "executable": True, "verdict": verdict.to_dict()}

    def reject(self, incident_id: str, action_id: str, *, rejected_by: str, reason: str = "") -> dict[str, Any]:
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            row = self._get_action(session, incident, action_id)
            row.status = ActionStatus.REJECTED.value
            row.decided_by = rejected_by
            row.decision_reason = reason
            row.decided_at = datetime.now(timezone.utc)
            event = record_event(
                session,
                incident_id=incident_id,
                phase=TimelinePhase.APPROVAL,
                title=f"Rejected by {rejected_by}: {row.description}",
                detail=reason or "No reason given.",
                actor=rejected_by,
                meta={"action_id": action_id, "risk": row.risk},
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        publish_event(incident_id, event)
        return {"action_id": action_id, "rejected": True}

    def record_action_state(
        self,
        incident_id: str,
        action_id: str,
        *,
        status: ActionStatus,
        decided_by: str = "agent",
        decision_reason: str = "",
    ) -> dict[str, Any]:
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            row = self._get_action(session, incident, action_id)
            row.status = status.value
            row.decided_by = decided_by
            row.decision_reason = decision_reason
            row.decided_at = datetime.now(timezone.utc)
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        return {"action_id": action_id, "status": status.value}

    def _get_action(self, session: Session, incident: orm.Incident, action_id: str) -> orm.ActionAttempt:
        row = session.get(orm.ActionAttempt, action_id)
        if row is None or row.incident_id != incident.id:
            raise ActionNotFound(f"unknown action '{action_id}' for incident {incident.id}")
        return row

    def _definition(self, row: orm.ActionAttempt) -> ActionDef:
        definition = get_action(definition_id_from_spec(row.id))
        if definition is None:
            # Fall back to a synthetic read-only definition for unknown ids.
            return ActionDef(
                id=definition_id_from_spec(row.id),
                description=row.description,
                type=row.type,  # type: ignore[arg-type]
                tool=row.tool or "get_incident_context",
                reason=row.reason or "",
                expected_signal=row.expected_signal or "",
                read_only=row.risk == RiskLevel_READ_ONLY,
                reversible=bool(row.reversible),
                data_loss_risk=bool(row.data_loss_risk),
                production_impact=bool(row.production_impact),
            )
        return definition

    # ------------------------------------------------------------------ execute
    async def execute_action(
        self,
        incident_id: str,
        action_id: str,
        *,
        approved_by: str = "",
        what_if_only: bool = False,
    ) -> dict[str, Any]:
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            scenario = self.scenario_for(incident)
            if scenario is None:
                raise IncidentNotFound(f"scenario for {incident_id} is missing")
            row = self._get_action(session, incident, action_id)
            definition = self._definition(row)
            sim = self._simulator(incident, scenario)

            if what_if_only:
                projection = sim.what_if(definition.id, read_only=definition.risk.value == RiskLevel_READ_ONLY)
                row.what_if = projection.model_dump(mode="json")
                row.predicted_outcome = projection.predicted_outcome.value
                row.predicted_detail = projection.predicted_detail
                session.commit()
                return {"action_id": action_id, "what_if": projection.model_dump(mode="json"), "executed": False}

            approved = row.status in {ActionStatus.APPROVED.value, ActionStatus.EXECUTED.value}
            verdict = self.safety.authorize_execution(
                definition,
                state=IncidentState(incident.state),
                approved=approved,
                approved_by=approved_by or row.decided_by or "",
            )
            if verdict.decision.value == "refuse":
                row.status = (ActionStatus.BLOCKED_BY_MEMORY.value if row.blocked_reason else row.status) if verdict.blockers and "high_risk_action_is_advisory_only" in verdict.blockers else row.status
                event = record_event(
                    session,
                    incident_id=incident_id,
                    phase=TimelinePhase.ACTION,
                    title=f"Refused by safety gate: {row.description}",
                    detail="; ".join(verdict.warnings) or "; ".join(verdict.blockers),
                    actor="safety-gate",
                    meta={"action_id": action_id, "verdict": verdict.to_dict()},
                    ts=sim.timestamp(),
                )
                session.commit()
                publish_event(incident_id, event)
                return {
                    "action_id": action_id,
                    "executed": False,
                    "refused": True,
                    "verdict": verdict.to_dict(),
                    "message": "Blocked by the safety gate. " + (" ".join(verdict.warnings) if verdict.warnings else ""),
                }
            if not verdict.authorized:
                event = record_event(
                    session,
                    incident_id=incident_id,
                    phase=TimelinePhase.APPROVAL,
                    title=f"Action requires approval: {row.description}",
                    detail="No approval recorded yet. The action was not executed.",
                    actor="safety-gate",
                    meta={"action_id": action_id, "verdict": verdict.to_dict()},
                    ts=sim.timestamp(),
                )
                session.commit()
                publish_event(incident_id, event)
                return {
                    "action_id": action_id,
                    "executed": False,
                    "requires_approval": True,
                    "verdict": verdict.to_dict(),
                    "message": "This action changes state and needs explicit human approval.",
                }

            events: list[Any] = []
            events.append(
                record_event(
                    session,
                    incident_id=incident_id,
                    phase=TimelinePhase.ACTION,
                    title=f"Executing ({definition.risk.value}): {row.description}",
                    detail=verdict.notes[0] if verdict.notes else row.reason,
                    actor=approved_by or row.decided_by or "agent",
                    meta={
                        "action_id": action_id,
                        "risk": definition.risk.value,
                        "tool": definition.tool,
                        "approved_by": approved_by or row.decided_by or "",
                    },
                    ts=sim.timestamp(),
                )
            )

            projection = sim.what_if(definition.id, read_only=definition.risk.value == RiskLevel_READ_ONLY)
            row.what_if = projection.model_dump(mode="json")
            row.predicted_outcome = projection.predicted_outcome.value
            row.predicted_detail = projection.predicted_detail

            execution = sim.apply_action(definition.id, read_only=definition.risk.value == RiskLevel_READ_ONLY)
            row.result = execution.result.model_dump(mode="json")
            row.executed_at = datetime.now(timezone.utc)
            row.status = ActionStatus.EXECUTED.value if execution.result.helped is not False else ActionStatus.FAILED.value
            if definition.risk.value == RiskLevel_READ_ONLY and execution.result.helped is None:
                row.status = ActionStatus.EXECUTED.value

            revealed = self._persist_evidence(session, incident, execution.revealed)
            if execution.metrics:
                self._persist_metric_samples(session, incident, execution.metrics, sim)
            self._persist_sim_state(incident, sim)
            incident.step_count = (incident.step_count or 0) + 1

            outcome_title = {
                Outcome.HELPED: "Action worked",
                Outcome.TEMPORARY: "Temporary improvement, then regression",
                Outcome.NO_EFFECT: "No measurable effect",
                Outcome.HURT: "Action made things worse",
            }[execution.result.outcome]
            events.append(
                record_event(
                    session,
                    incident_id=incident_id,
                    phase=TimelinePhase.OUTCOME,
                    title=f"Outcome: {outcome_title} - {row.description}",
                    detail=execution.result.detail,
                    actor="simulator",
                    meta={
                        "action_id": action_id,
                        "outcome": execution.result.outcome.value,
                        "helped": execution.result.helped,
                        "lesson": execution.lesson(),
                        "observed": {k: execution.state.metrics.get(k) for k in list(execution.state.metrics)[:8]},
                        "resolves": execution.resolves,
                    },
                    ts=sim.timestamp(),
                )
            )
            if execution.lesson():
                events.append(
                    record_event(
                        session,
                        incident_id=incident_id,
                        phase=TimelinePhase.OUTCOME,
                        title="Reusable lesson captured from this action",
                        detail=execution.lesson(),
                        actor="simulator",
                        meta={"action_id": action_id, "lesson": execution.lesson()},
                        ts=sim.timestamp(),
                    )
                )

            if execution.resolves:
                self._confirm_root_cause(session, incident, scenario, sim, events)

            if incident.state == IncidentState.INVESTIGATING.value and execution.resolves:
                self._transition(incident, IncidentState.MITIGATED)
                self._transition(incident, IncidentState.MONITORING)

            session.commit()
        except SafetyViolation:
            session.rollback()
            raise
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        for event in events:
            publish_event(incident_id, event)

        payload: dict[str, Any] = {
            "action_id": action_id,
            "executed": True,
            "outcome": execution.result.model_dump(mode="json"),
            "resolves": execution.resolves,
            "stage": execution.state.snapshot(),
            "new_evidence": [e.id for e in revealed],
            "lesson": execution.lesson(),
            "verdict": verdict.to_dict(),
        }
        if revealed or execution.resolves:
            payload["analysis"] = await self.analyze(incident_id, reason="re-analysed after the action outcome")
        return payload

    def _confirm_root_cause(
        self,
        session: Session,
        incident: orm.Incident,
        scenario: Scenario,
        sim: ScenarioSimulator,
        events: list[Any],
    ) -> None:
        gt = scenario.ground_truth
        incident.root_cause_id = gt.root_cause_id
        incident.root_cause = gt.root_cause
        incident.resolution = gt.resolution
        incident.resolved_at = scenario.timestamp(sim.offset_s)
        incident.confirmed_step = incident.step_count or 0
        hyp = session.query(orm.Hypothesis).filter(orm.Hypothesis.incident_id == incident.id, orm.Hypothesis.cause_id == gt.root_cause_id).first()
        if hyp is not None:
            hyp.confirmed = True
            for other in session.query(orm.Hypothesis).filter(orm.Hypothesis.incident_id == incident.id).all():
                if other.id != hyp.id:
                    other.confirmed = False
        events.append(
            record_event(
                session,
                incident_id=incident.id,
                phase=TimelinePhase.RESOLUTION,
                title=f"Root cause confirmed: {gt.root_cause}",
                detail=gt.resolution,
                actor="agent",
                meta={"root_cause_id": gt.root_cause_id, "verification": gt.verification},
                ts=sim.timestamp(),
            )
        )

    # ------------------------------------------------------------------ resolve
    def resolve(
        self,
        incident_id: str,
        *,
        root_cause_id: str | None = None,
        resolution: str | None = None,
        resolved_by: str = "incident-commander",
    ) -> dict[str, Any]:
        session = self.session()
        events: list[Any] = []
        try:
            incident = self.get_incident(session, incident_id)
            scenario = self.scenario_for(incident)
            if scenario is None:
                raise IncidentNotFound(f"scenario for {incident_id} is missing")
            sim = self._simulator(incident, scenario)
            record = record_from_orm(incident, scenario=scenario)
            record.stage_id = sim.stage_id
            record.sim_metrics = sim.metrics

            if root_cause_id:
                incident.root_cause_id = root_cause_id
                incident.root_cause = cause_name(root_cause_id)
            elif not incident.root_cause_id:
                top = record.top_hypothesis
                incident.root_cause_id = top.cause_id if top else ""
                incident.root_cause = top.cause if top else "Unconfirmed"
            if resolution:
                incident.resolution = resolution
            elif not incident.resolution and scenario.ground_truth.resolution:
                incident.resolution = scenario.ground_truth.resolution
            if incident.resolved_at is None:
                incident.resolved_at = scenario.timestamp(sim.offset_s)
            incident.confirmed_step = incident.confirmed_step or incident.step_count or 0
            if incident.state not in {IncidentState.CLOSED.value, IncidentState.LEARNED.value}:
                if can_transition(incident.state, IncidentState.RESOLVED):
                    self._transition(incident, IncidentState.RESOLVED)
                else:
                    # e.g. resolving straight from NEW: record the skip explicitly.
                    self._transition(incident, IncidentState.RESOLVED, force=True)

            hyp = session.query(orm.Hypothesis).filter(
                orm.Hypothesis.incident_id == incident.id, orm.Hypothesis.cause_id == incident.root_cause_id
            ).first()
            if hyp is not None:
                hyp.confirmed = True

            # Rebuild the record so the postmortem sees the confirmed state.
            session.flush()
            session.refresh(incident)
            record = record_from_orm(incident, scenario=scenario)
            record.stage_id = sim.stage_id
            record.sim_metrics = sim.metrics
            postmortem = build_postmortem(record)
            runbook = build_runbook(record, postmortem=postmortem)

            existing_pm = session.query(orm.Postmortem).filter(orm.Postmortem.incident_id == incident.id).first()
            if existing_pm is not None:
                session.delete(existing_pm)
                session.flush()
            pm_row = orm.Postmortem(
                id=f"{incident.id}-pm",
                incident_id=incident.id,
                title=postmortem.title,
                severity=postmortem.severity.value,
                service=postmortem.service,
                summary=postmortem.summary,
                impact=postmortem.impact,
                root_cause=postmortem.root_cause,
                contributing_factors=postmortem.contributing_factors,
                actions_taken=[a.model_dump(mode="json") for a in postmortem.actions_taken],
                failed_actions=[a.model_dump(mode="json") for a in postmortem.failed_actions],
                successful_actions=[a.model_dump(mode="json") for a in postmortem.successful_actions],
                resolution=postmortem.resolution,
                prevention=postmortem.prevention,
                runbook_changes=postmortem.runbook_changes,
                lessons=postmortem.lessons,
                evidence=[e.model_dump(mode="json") for e in postmortem.evidence],
                timeline=[e.model_dump(mode="json") for e in postmortem.timeline],
            )
            session.add(pm_row)
            self._upsert_runbook(session, runbook, cause_id=incident.root_cause_id)
            postmortem.memory_written = False

            events.append(
                record_event(
                    session,
                    incident_id=incident.id,
                    phase=TimelinePhase.RESOLUTION,
                    title="Postmortem generated",
                    detail=postmortem.summary,
                    actor="postmortem-builder",
                    meta={"postmortem_id": pm_row.id, "runbook_id": runbook.id, "sections": 12},
                    ts=sim.timestamp(),
                )
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        for event in events:
            publish_event(incident_id, event)

        retained = self.retain_incident_memory(incident_id)
        postmortem = self.get_postmortem(incident_id)
        return {
            "incident_id": incident_id,
            "state": IncidentState.RESOLVED.value,
            "root_cause": (postmortem or {}).get("root_cause") or incident.root_cause,
            "postmortem": postmortem,
            "memory": retained,
            "runbook": self.get_runbook_for_incident(incident_id),
            "resolved_by": resolved_by,
        }

    def _upsert_runbook(self, session: Session, runbook: Any, *, cause_id: str = "") -> None:
        row = session.get(orm.Runbook, runbook.id)
        if row is None:
            row = orm.Runbook(id=runbook.id)
            session.add(row)
        row.service = runbook.service
        row.title = runbook.title
        row.cause_id = cause_id
        row.symptoms = runbook.symptoms
        row.first_checks = runbook.first_checks
        row.diagnostics = runbook.diagnostics
        row.known_failed_actions = runbook.known_failed_actions
        row.recommended_actions = runbook.recommended_actions
        row.verification = runbook.verification
        row.rollback = runbook.rollback
        row.prevention = runbook.prevention
        row.steps = [s.model_dump(mode="json") for s in runbook.steps]
        source = set(row.source_incidents or [])
        source.update(runbook.source_incidents)
        row.source_incidents = sorted(source)

    # ------------------------------------------------------------------ memory write
    def retain_incident_memory(self, incident_id: str, *, include_diagnostics: bool = True) -> dict[str, Any]:
        if self.memory is None:
            return {"written": 0, "detail": "memory layer is disabled"}
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            scenario = self.scenario_for(incident)
            record = record_from_orm(incident, scenario=scenario)
            pm_row = session.query(orm.Postmortem).filter(orm.Postmortem.incident_id == incident_id).first()
            postmortem = None
            if pm_row is not None:
                from recallops.domain.models import PostmortemReport

                postmortem = PostmortemReport(
                    incident_id=pm_row.incident_id,
                    title=pm_row.title,
                    severity=pm_row.severity,  # type: ignore[arg-type]
                    service=pm_row.service,
                    summary=pm_row.summary,
                    impact=pm_row.impact,
                    timeline=[],
                    root_cause=pm_row.root_cause,
                    contributing_factors=pm_row.contributing_factors,
                    evidence=[],
                    actions_taken=[],
                    failed_actions=[],
                    successful_actions=[],
                    resolution=pm_row.resolution,
                    prevention=pm_row.prevention,
                    runbook_changes=pm_row.runbook_changes,
                    lessons=pm_row.lessons,
                )
            composition = compose_from_incident(record, postmortem=postmortem)
            if not include_diagnostics:
                composition.candidates = [
                    c for c in composition.candidates
                    if not (c.item.kind is MemoryKind.ACTION_OUTCOME and c.item.action_id and c.item.action_id.startswith("inspect_"))
                ]
            receipt = self.memory.retain(composition.accepted, scope=record.service)

            stored_ids: list[str] = []
            for item in composition.accepted:
                # The local mirror may already have written this row during
                # ``memory.retain``; upsert so both paths converge on one record.
                row = session.get(orm.MemoryRecord, item.id)
                if row is None:
                    row = orm.MemoryRecord(id=item.id)
                    session.add(row)
                row.incident_id = incident.id
                row.kind = item.kind.value
                row.durability = item.durability.value
                row.title = item.title[:255]
                row.content = item.content
                row.service = item.service
                row.cause_id = item.cause_id
                row.action_id = item.action_id
                row.outcome = item.outcome
                row.score = item.score
                row.source = receipt.mode.value
                row.external_id = item.external_id
                row.memory_type = item.memory_type
                row.entities = item.entities
                row.tags = item.tags
                row.meta = {
                    **(row.meta or {}),
                    "helped": item.helped,
                    "expected_signal": item.expected_signal,
                    "actual_outcome": item.actual_outcome,
                    "lesson": item.lesson,
                    "reusable_lesson": item.reusable_lesson,
                    "document_id": f"recallops:{item.kind.value}:{item.incident_id}:{item.id[:24]}",
                }
                row.created_at = item.occurred_at or row.created_at or datetime.now(timezone.utc)
                stored_ids.append(item.id)

            if pm_row is not None:
                pm_row.memory_written = True
                pm_row.memory_written_count = receipt.written
                pm_row.memory_ids = stored_ids

            if incident.state == IncidentState.RESOLVED.value:
                self._transition(incident, IncidentState.LEARNED)
            incident.meta = {
                **(incident.meta or {}),
                "memory_summary": composition.summary(),
                "memory_mode_used": receipt.mode.value,
            }
            event = record_event(
                session,
                incident_id=incident_id,
                phase=TimelinePhase.MEMORY,
                title=(
                    f"New organisational memory created: {receipt.written} durable memories retained via {receipt.mode.value}"
                    if receipt.written
                    else "No new durable memory written this run"
                ),
                detail="; ".join(c.title[:90] for c in composition.accepted[:4]),
                actor="memory",
                meta={
                    "written": receipt.written,
                    "rejected": composition.summary().get("rejected"),
                    "mode": receipt.mode.value,
                    "degraded": receipt.degraded,
                    "kinds": composition.summary().get("kinds"),
                },
                ts=record.resolved_at or datetime.now(timezone.utc),
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()
        publish_event(incident_id, event)
        return {
            "written": receipt.written,
            "rejected": receipt.rejected,
            "mode": receipt.mode.value,
            "degraded": receipt.degraded,
            "ids": stored_ids,
            "quality": composition.summary(),
            "details": receipt.details,
        }

    # ------------------------------------------------------------------ feedback
    def record_feedback(
        self,
        incident_id: str,
        *,
        target_type: str,
        target_id: str,
        verdict: str,
        comment: str = "",
        corrected_cause: str = "",
        author: str = "oncall",
    ) -> dict[str, Any]:
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            row = orm.EngineerFeedback(
                id=f"{incident_id}-fb{uuid.uuid4().hex[:8]}",
                incident_id=incident_id,
                target_type=target_type,
                target_id=target_id,
                verdict=verdict,
                comment=comment,
                corrected_cause=corrected_cause,
                author=author,
            )
            session.add(row)
            if target_type == "hypothesis" and verdict in {"incorrect", "wrong"}:
                hyp = session.query(orm.Hypothesis).filter(orm.Hypothesis.id == target_id).first()
                if hyp is not None:
                    hyp.rejected = True
                    hyp.rejected_reason = comment or "rejected by the on-call engineer"
            record_event(
                session,
                incident_id=incident_id,
                phase=TimelinePhase.MEMORY_RECALL,
                title=f"Engineer feedback on {target_type}: {verdict}",
                detail=comment or corrected_cause or "",
                actor=author,
                meta={"target_id": target_id, "corrected_cause": corrected_cause},
            )
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

        result: dict[str, Any] = {"recorded": True, "target_id": target_id, "verdict": verdict}
        if corrected_cause:
            composed = compose_correction(
                incident_id=incident_id,
                service=incident.service,
                rejected_hypothesis=target_id.split("::")[-1] if "::" in target_id else target_id,
                corrected_cause=corrected_cause,
                comment=comment,
                author=author,
            )
            if composed.accepted and self.memory is not None:
                receipt = self.memory.retain([composed.item], scope=incident.service)
                result["correction_memory"] = {
                    "written": receipt.written,
                    "mode": receipt.mode.value,
                    "item": composed.item.model_dump(mode="json"),
                }
            else:
                result["correction_memory"] = {
                    "written": 0,
                    "rejected_reason": composed.item.rejected_reason or "; ".join(composed.verdict.reasons),
                }
        return result

    # ------------------------------------------------------------------ reads
    def what_if(self, incident_id: str, definition_id: str) -> dict[str, Any]:
        session = self.session()
        try:
            incident = self.get_incident(session, incident_id)
            scenario = self.scenario_for(incident)
            if scenario is None:
                raise IncidentNotFound(f"scenario for {incident_id} is missing")
            sim = self._simulator(incident, scenario)
            definition = get_action(definition_id)
            read_only = bool(definition and definition.risk.value == RiskLevel_READ_ONLY)
            return sim.what_if(definition_id, read_only=read_only).model_dump(mode="json")
        finally:
            session.close()

    def get_postmortem(self, incident_id: str) -> dict[str, Any] | None:
        session = self.session()
        try:
            row = session.query(orm.Postmortem).filter(orm.Postmortem.incident_id == incident_id).first()
            if row is None:
                return None
            return {
                "id": row.id,
                "incident_id": row.incident_id,
                "title": row.title,
                "severity": row.severity,
                "service": row.service,
                "summary": row.summary,
                "impact": row.impact,
                "root_cause": row.root_cause,
                "contributing_factors": row.contributing_factors,
                "actions_taken": row.actions_taken,
                "failed_actions": row.failed_actions,
                "successful_actions": row.successful_actions,
                "resolution": row.resolution,
                "prevention": row.prevention,
                "runbook_changes": row.runbook_changes,
                "lessons": row.lessons,
                "evidence": row.evidence,
                "timeline": row.timeline,
                "memory_written": row.memory_written,
                "memory_written_count": row.memory_written_count,
                "memory_ids": row.memory_ids,
                "generated_at": row.generated_at.isoformat() if row.generated_at else None,
            }
        finally:
            session.close()

    def get_runbook_for_incident(self, incident_id: str) -> dict[str, Any] | None:
        session = self.session()
        try:
            incident = session.get(orm.Incident, incident_id)
            cause_id = incident.root_cause_id if incident else ""
            service = incident.service if incident else ""
            row = (
                session.query(orm.Runbook)
                .filter(orm.Runbook.service == service)
                .order_by(orm.Runbook.updated_at.desc())
                .first()
            )
            if row is None:
                return None
            return {
                "id": row.id,
                "title": row.title,
                "service": row.service,
                "cause_id": row.cause_id or cause_id,
                "symptoms": row.symptoms,
                "first_checks": row.first_checks,
                "diagnostics": row.diagnostics,
                "known_failed_actions": row.known_failed_actions,
                "recommended_actions": row.recommended_actions,
                "verification": row.verification,
                "rollback": row.rollback,
                "prevention": row.prevention,
                "steps": row.steps,
                "source_incidents": row.source_incidents,
                "created_at": row.created_at.isoformat() if row.created_at else None,
                "updated_at": row.updated_at.isoformat() if row.updated_at else None,
            }
        finally:
            session.close()

    def replay(self, incident_id: str) -> dict[str, Any]:
        record = self.load_record(incident_id)
        if record is None:
            raise IncidentNotFound(f"unknown incident '{incident_id}'")
        return {
            "incident_id": incident_id,
            "service": record.service,
            "severity": record.severity,
            "state": record.state,
            "root_cause": record.root_cause,
            "memory_mode": record.memory_mode,
            "steps": record.step_count,
            "timeline": [e.model_dump(mode="json") for e in record.timeline()],
            "hypotheses": [h.model_dump(mode="json") for h in record.ranked_hypotheses],
            "actions": [
                {
                    "id": a.id,
                    "description": a.description,
                    "risk": a.risk.value,
                    "status": a.status.value,
                    "expected_signal": a.expected_signal,
                    "outcome": a.result.model_dump(mode="json") if a.result else None,
                    "lesson": a.result.lesson if a.result else None,
                    "blocked_reason": a.blocked_reason,
                }
                for a in record.actions
            ],
            "summary": {
                "diagnostics_run": len(record.diagnostics()),
                "remediations_attempted": len(record.remediation_attempts()),
                "failed_actions": len(record.failed_actions()),
                "successful_actions": len(record.successful_actions()),
                "blocked_by_memory": len(record.blocked_actions()),
                "memory_recalled": len(record.recalled_memory_ids),
                "memory_assisted": record.memory_assisted,
            },
        }

    # ------------------------------------------------------------------ internals
    def _bundle(self, record: IncidentRecord, scenario: Scenario, sim: ScenarioSimulator) -> EvidenceBundle:
        """Assemble the RCA input from persisted evidence plus live simulator state."""
        recent = scenario.recent_deployment()
        return EvidenceBundle(
            service=record.service,
            incident_id=record.id,
            symptom=record.symptom,
            evidence=record.evidence,
            deploy_versions=[d.get("version", "") for d in record.deployments],
            recent_deploy_version=recent.version if recent else None,
            recent_deploy_minutes=scenario.deployment_minutes_before(recent) if recent else None,
            service_criticality=record.impact.service_criticality if record.impact else "standard",
            customer_impact=record.impact.customer_impact if record.impact else "low",
            memory_mode=record.memory_mode,
            stage=sim.stage_id,
        )

    def _simulator(self, incident: orm.Incident, scenario: Scenario) -> ScenarioSimulator:
        meta = incident.meta or {}
        sim = ScenarioSimulator(
            scenario,
            stage_id=str(meta.get("stage_id") or scenario.first_stage_id),
            offset_s=float(meta.get("offset_s", 0) or 0),
            incident_id=incident.id,
        )
        for name, value in (meta.get("sim_metrics") or {}).items():
            sim.metrics[name] = float(value)
        return sim

    def _persist_sim_state(self, incident: orm.Incident, sim: ScenarioSimulator) -> None:
        incident.meta = {
            **(incident.meta or {}),
            "stage_id": sim.stage_id,
            "offset_s": sim.offset_s,
            "sim_metrics": sim.metrics,
            "resolved": sim.resolved,
        }

    def _transition(self, incident: orm.Incident, target: IncidentState, *, force: bool = False) -> None:
        current = IncidentState(incident.state)
        if current is target:
            return
        if force:
            incident.state = target.value
            return
        try:
            incident.state = assert_transition(current, target).value
        except Exception as exc:  # noqa: BLE001
            raise InvalidTransition(str(exc)) from exc

    def _persist_evidence(self, session: Session, incident: orm.Incident, evidence: Sequence[EvidenceRef]) -> list[EvidenceRef]:
        stored: list[EvidenceRef] = []
        base_seq = max((e.seq or 0 for e in incident.evidence), default=0)
        for offset, ev in enumerate(evidence, start=1):
            # Cheap primary-key probe: evidence is immutable once stored.
            if session.get(orm.EvidenceEvent, ev.id) is not None:
                continue
            session.add(
                orm.EvidenceEvent(
                    id=ev.id,
                    incident_id=incident.id,
                    seq=base_seq + offset,
                    kind=ev.kind.value,
                    source=ev.source,
                    title=ev.title[:500],
                    detail=ev.detail,
                    ts=ev.ts or datetime.now(timezone.utc),
                    stage=ev.stage or "",
                    signal_hints=ev.signal_hints,
                    raw=ev.raw,
                    untrusted=ev.untrusted,
                    redacted=ev.redacted,
                )
            )
            stored.append(ev)
        return stored

    def _persist_metric_samples(self, session: Session, incident: orm.Incident, metrics: Sequence[Any], sim: ScenarioSimulator) -> None:
        ts = sim.timestamp()
        for metric in metrics:
            session.add(
                orm.MetricSample(
                    incident_id=incident.id,
                    ts=ts,
                    name=metric.name,
                    value=float(metric.value),
                    unit=metric.unit,
                    limit_value=metric.limit,
                    baseline=metric.baseline,
                    stage=metric.stage or sim.stage_id,
                )
            )

    def _persist_stage_data(
        self,
        session: Session,
        incident: orm.Incident,
        scenario: Scenario,
        sim: ScenarioSimulator,
        *,
        include_logs: bool = True,
    ) -> None:
        stage = sim.stage
        ts = sim.timestamp()
        existing_evidence = {e.id for e in incident.evidence}

        evidence: list[EvidenceRef] = []
        for metric in scenario.initial_metrics():
            evidence.append(normalize_metric(metric, incident.id, stage.id, sim.timestamp(metric.ts_offset_s)))
        if include_logs:
            for log in stage.logs:
                evidence.append(normalize_log(log, incident.id, stage.id, sim.timestamp(log.ts_offset_s)))
        for item in stage.evidence:
            from recallops.agent.evidence import normalize_stage_evidence

            evidence.append(normalize_stage_evidence(item, incident.id, stage.id, sim.timestamp(item.ts_offset_s)))
        self._persist_evidence(session, incident, [e for e in evidence if e.id not in existing_evidence])

        if include_logs:
            for log in stage.logs:
                session.add(
                    orm.LogEvent(
                        id=f"{incident.id}-log-{_stable_id(log.ts_offset_s, log.message)}",
                        incident_id=incident.id,
                        ts=sim.timestamp(log.ts_offset_s),
                        level=log.level,
                        logger=log.logger,
                        message=redact_text(log.message)[:900],
                        count=log.count,
                        stage=stage.id,
                    )
                )
        for metric in scenario.initial_metrics():
            session.add(
                orm.MetricSample(
                    incident_id=incident.id,
                    ts=ts,
                    name=metric.name,
                    value=metric.value,
                    unit=metric.unit,
                    limit_value=metric.limit,
                    baseline=metric.baseline,
                    stage=stage.id,
                )
            )

    def _persist_context_rows(self, session: Session, incident: orm.Incident, scenario: Scenario) -> None:
        for deployment in scenario.deployments:
            minutes = scenario.deployment_minutes_before(deployment)
            in_window = deployment.deployed_at_offset_s >= -3600
            correlation = 0.0
            if in_window:
                correlation = round(max(0.4, 0.9 - minutes / 120.0), 3)
            elif minutes <= 4320:
                correlation = 0.1
            session.add(
                orm.Deployment(
                    id=f"{incident.id}-dep-{deployment.version}",
                    incident_id=incident.id,
                    service=scenario.service,
                    version=deployment.version,
                    deployed_at=scenario.timestamp(deployment.deployed_at_offset_s),
                    minutes_before_incident=round(minutes, 1),
                    change_summary=deployment.change_summary,
                    author=deployment.author,
                    status=deployment.status,
                    risk=deployment.risk,
                    correlation=correlation,
                    meta={"tags": deployment.tags, "note": deployment.note, "files_changed": deployment.files_changed},
                )
            )
        for dependency in scenario.dependencies:
            session.add(
                orm.ServiceDependency(
                    id=f"{incident.id}-dep-{dependency.name}",
                    incident_id=incident.id,
                    name=dependency.name,
                    kind=dependency.kind,
                    direction=dependency.direction,
                    status=dependency.status,
                    criticality=dependency.criticality,
                    latency_p95_ms=dependency.latency_p95_ms,
                    error_rate_pct=dependency.error_rate_pct,
                    meta={"note": dependency.note},
                )
            )


RiskLevel_READ_ONLY = "READ_ONLY"


def _baseline_latency(scenario: Scenario) -> float | None:
    for metric in scenario.metrics:
        if metric.name in {"latency_p95_ms", "checkout_latency_p95_ms"} and metric.baseline:
            return float(metric.baseline)
    return None


def _stable_id(*parts: object) -> str:
    """Deterministic id fragment (``hash()`` is salted per process, this is not)."""
    raw = "|".join(str(p) for p in parts).encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:10]


__all__ = [
    "ActionNotFound",
    "IncidentNotFound",
    "IncidentOrchestrator",
    "InvalidTransition",
]
