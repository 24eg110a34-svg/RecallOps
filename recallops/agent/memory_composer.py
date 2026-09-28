"""Memory composition: turn a resolved incident into durable organisational learning.

Produces the five categories required by the product spec:

1. ``incident_episode``    - what happened, confirmed cause, resolution, what to check first
2. ``action_outcome``      - every action attempted, with its real outcome and lesson
3. ``runbook_lesson``      - useful / unnecessary / dangerous / missing steps
4. ``service_pattern``     - recurring dependency, characteristic failure, deploy relationship
5. ``postmortem_lesson``   - prevention + contributing factors
6. ``engineer_correction`` - rejected hypotheses and corrections (added separately)

Only *confirmed* outcomes are composed as durable facts. Each candidate passes the
quality filter before it is offered to the memory layer, and rejections are
reported so the UI can show what was deliberately not learned.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Sequence

from recallops.agent.postmortem import build_runbook
from recallops.agent.record import IncidentRecord
from recallops.domain.enums import Durability, MemoryKind, Outcome
from recallops.domain.models import PostmortemReport
from recallops.domain.signals import CAUSES, cause_name
from recallops.memory.models import MemoryItem
from recallops.memory.quality import QualityVerdict, evaluate


@dataclass
class ComposedMemory:
    item: MemoryItem
    verdict: QualityVerdict

    @property
    def accepted(self) -> bool:
        return self.verdict.accepted


@dataclass
class CompositionResult:
    candidates: list[ComposedMemory] = field(default_factory=list)

    @property
    def accepted(self) -> list[MemoryItem]:
        return [c.item for c in self.candidates if c.accepted]

    @property
    def rejected(self) -> list[ComposedMemory]:
        return [c for c in self.candidates if not c.accepted]

    def summary(self) -> dict[str, object]:
        return {
            "candidates": len(self.candidates),
            "accepted": len(self.accepted),
            "rejected": len(self.rejected),
            "rejections": [
                {"title": c.item.title, "reasons": c.verdict.reasons} for c in self.rejected
            ],
            "kinds": sorted({c.item.kind.value for c in self.candidates if c.accepted}),
        }


def _now(record: IncidentRecord) -> datetime:
    return record.resolved_at or record.detected_at or datetime.now(timezone.utc)


def compose_from_incident(record: IncidentRecord, postmortem: PostmortemReport | None = None) -> CompositionResult:
    """Build every memory candidate for a resolved incident."""
    result = CompositionResult()
    gt = record.scenario.ground_truth if record.scenario else None
    cause_id = record.root_cause_id or (record.confirmed_hypothesis.cause_id if record.confirmed_hypothesis else "")
    cause = CAUSES.get(cause_id)
    service = record.service
    when = _now(record)
    tags = [t for t in (record.scenario.meta.tags if record.scenario else []) if t][:6]

    # 1) incident episode -------------------------------------------------
    symptoms = _symptom_lines(record)
    evidence_lines = [f"{e.title}" for e in record.evidence if e.kind.value in {"alert", "metric", "deployment", "log"}][:6]
    first_checks = list(gt.first_checks) if gt and gt.first_checks else (list(cause.diagnostics)[:3] if cause else [])
    episode = MemoryItem(
        id=f"{record.id}~episode",
        kind=MemoryKind.INCIDENT_EPISODE,
        title=f"{record.id}: {cause_name(cause_id) if cause else record.root_cause or 'incident'} on {service}",
        content=(
            f"{record.severity} incident on {service}. Symptoms: {record.symptom} "
            f"Key evidence: {'; '.join(evidence_lines[:4])}. "
            f"Confirmed root cause: {record.root_cause or cause_name(cause_id)}. "
            f"Resolution: {record.resolution or (gt.resolution if gt else '')}. "
            f"Check first next time: {'; '.join(first_checks[:3])}."
        ),
        service=service,
        incident_id=record.id,
        cause_id=cause_id,
        memory_type="world",
        occurred_at=when,
        tags=[*tags, "episode", service.lower()],
        entities=[service, record.id, cause_name(cause_id)] if cause else [service, record.id],
        meta={
            "symptoms": symptoms,
            "severity": record.severity,
            "confirmed": bool(record.root_cause_id or record.confirmed_hypothesis),
            "resolution": record.resolution,
        },
        reusable_lesson="; ".join(first_checks[:3]),
    )
    result.candidates.append(_screen(episode))

    # 2) action outcomes --------------------------------------------------
    for action in record.executed_actions():
        result_memory_type = "experience"
        helped = action.result.helped if action.result else None
        outcome_value = action.result.outcome.value if action.result else "no_effect"
        lesson = (action.result.lesson if action.result else "") or ""
        item = MemoryItem(
            id=f"{record.id}~{action.id.rsplit('~', 1)[-1]}",
            kind=MemoryKind.ACTION_OUTCOME,
            title=(
                f"{action.description} on {service} -> "
                f"{'helped' if helped else 'failed' if helped is False else 'inconclusive'}"
            ),
            content=(action.result.detail if action.result else action.reason)[:900],
            service=service,
            incident_id=record.id,
            cause_id=cause_id,
            action_id=action.id.rsplit("~", 1)[-1],
            action=action.description,
            outcome=outcome_value,
            helped=helped,
            expected_signal=action.expected_signal,
            actual_outcome=(action.result.detail if action.result else "")[:600],
            lesson=lesson,
            reusable_lesson=_reusable_lesson(action, record),
            memory_type=result_memory_type,
            occurred_at=when,
            tags=[*tags, "action-outcome", outcome_value],
            entities=[service, action.type.value],
            meta={"risk": action.risk.value, "required_approval": action.requires_confirmation},
        )
        result.candidates.append(_screen(item))

    # 3) runbook lesson ---------------------------------------------------
    if record.scenario:
        runbook = build_runbook(record, postmortem=postmortem)
        useful = list(runbook.first_checks)
        unnecessary = [a.description for a in record.executed_actions() if a.risk == "READ_ONLY" and a.result and a.result.outcome is Outcome.NO_EFFECT]
        dangerous = [f"{a.description} - {a.result.detail}"[:240] for a in record.failed_actions() if a.result and a.result.helped is not True]
        missing = list(gt.runbook_changes) if gt else []
        item = MemoryItem(
            id=f"{record.id}~runbook",
            kind=MemoryKind.RUNBOOK_LESSON,
            title=f"Runbook lesson for {service} ({cause_name(cause_id) if cause else 'incident'})",
            content=(
                f"Useful steps: {'; '.join(useful[:4])}. "
                f"Unnecessary steps: {'; '.join(unnecessary[:3]) or 'none recorded'}. "
                f"Dangerous steps: {'; '.join(dangerous[:2]) or 'none recorded'}. "
                f"Missing steps to add: {'; '.join(missing[:3]) or 'none recorded'}."
            ),
            service=service,
            incident_id=record.id,
            cause_id=cause_id,
            memory_type="world",
            occurred_at=when,
            tags=[*tags, "runbook"],
            entities=[service],
            reusable_lesson=useful[0] if useful else "",
            meta={
                "useful": useful,
                "unnecessary": unnecessary,
                "dangerous": dangerous,
                "missing": missing,
                "runbook_id": runbook.id,
            },
        )
        result.candidates.append(_screen(item))

    # 4) service pattern ---------------------------------------------------
    pattern_text = gt.memory_service_pattern if gt and gt.memory_service_pattern else _fallback_pattern(record, cause_id)
    if pattern_text:
        item = MemoryItem(
            id=f"{record.id}~pattern",
            kind=MemoryKind.SERVICE_PATTERN,
            title=f"{service}: recurring failure pattern - {cause_name(cause_id) if cause else 'incident'}",
            content=pattern_text,
            service=service,
            incident_id=record.id,
            cause_id=cause_id,
            memory_type="world",
            occurred_at=when,
            tags=[*tags, "service-pattern"],
            entities=[service, *[str(d.get("name", "")) for d in record.dependencies[:3] if d.get("name")]],
            reusable_lesson=pattern_text.split(". ")[0][:220],
            meta={"occurrences": 1, "dependency": [d.get("name") for d in record.dependencies[:3]]},
        )
        result.candidates.append(_screen(item))

    # 5) postmortem lesson -------------------------------------------------
    if postmortem:
        item = MemoryItem(
            id=f"{record.id}~postmortem",
            kind=MemoryKind.POSTMORTEM_LESSON,
            title=f"{record.id} postmortem: prevention and contributing factors",
            content=(
                f"Contributing factors: {'; '.join(postmortem.contributing_factors[:3])}. "
                f"Prevention: {'; '.join(postmortem.prevention[:3])}. "
                f"Lessons: {'; '.join(postmortem.lessons[:3])}."
            ),
            service=service,
            incident_id=record.id,
            cause_id=cause_id,
            memory_type="observation",
            occurred_at=when,
            tags=[*tags, "postmortem"],
            entities=[service],
            reusable_lesson=(postmortem.lessons[0] if postmortem.lessons else "")[:220],
            meta={"prevention": postmortem.prevention, "contributing": postmortem.contributing_factors},
        )
        result.candidates.append(_screen(item))

    return result


def compose_correction(
    *,
    incident_id: str,
    service: str,
    rejected_hypothesis: str,
    corrected_cause: str,
    comment: str = "",
    author: str = "oncall",
    when: datetime | None = None,
) -> ComposedMemory:
    item = MemoryItem(
        id=f"{incident_id}~correction~{abs(hash((rejected_hypothesis, corrected_cause))) % 100000}",
        kind=MemoryKind.ENGINEER_CORRECTION,
        title=f"Correction on {incident_id}: {rejected_hypothesis} was rejected",
        content=(
            f"Rejected hypothesis: {rejected_hypothesis}. Corrected cause: {corrected_cause}. "
            f"Clarified context: {comment or 'n/a'} (reported by {author})."
        ),
        service=service,
        incident_id=incident_id,
        memory_type="observation",
        occurred_at=when or datetime.now(timezone.utc),
        tags=["correction", service.lower()],
        entities=[service, rejected_hypothesis[:40], corrected_cause[:40]],
        reusable_lesson=f"{rejected_hypothesis} was rejected; the actual cause was {corrected_cause}.",
    )
    return _screen(item)


def _screen(item: MemoryItem) -> ComposedMemory:
    verdict = evaluate(item)
    item.durability = verdict.durability
    if not verdict.accepted:
        item.rejected_reason = "; ".join(verdict.reasons)
    return ComposedMemory(item=item, verdict=verdict)


def _reusable_lesson(action, record: IncidentRecord) -> str:
    """A lesson is only recorded when the action taught us something.

    A read-only check that changed nothing is still worth remembering as a
    low-value check for this symptom; a remediation that regressed is worth a
    hard rule.
    """
    if action.result and action.result.lesson:
        return action.result.lesson
    if action.risk == "READ_ONLY":
        return (
            f"Diagnostic '{action.description}' produced no change and no decisive signal for this symptom; "
            "treat it as a low-value primary check."
        )
    return ""


def _symptom_lines(record: IncidentRecord) -> list[str]:
    return [e.title for e in record.evidence if e.kind.value in {"alert", "log"}][:5]


def _fallback_pattern(record: IncidentRecord, cause_id: str) -> str:
    deps = ", ".join(d.get("name", "") for d in record.dependencies[:3] if d.get("name"))
    cause = CAUSES.get(cause_id)
    return (
        f"{record.service} has failed as {cause_name(cause_id) if cause else 'an incident'} with the signature: "
        f"{record.symptom[:160]}. Key dependencies: {deps}."
    )


def compose_for_incident(record: IncidentRecord, postmortem: PostmortemReport | None = None) -> CompositionResult:
    return compose_from_incident(record, postmortem=postmortem)


__all__ = [
    "ComposedMemory",
    "CompositionResult",
    "compose_correction",
    "compose_for_incident",
    "compose_from_incident",
]
