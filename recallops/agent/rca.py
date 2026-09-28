"""Root cause engine.

Pipeline: evidence -> signals (named facts) -> candidate causes -> scored
hypotheses. The scoring is deterministic and every component is inspectable, so
two engineers reading the same output reach the same conclusion, and so do the
tests. Memory contributes a *prior and a warning*, never the proof.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from recallops.domain.enums import EvidenceKind, MemoryKind
from recallops.domain.models import EvidenceRef, MemoryConflict, MemoryLink
from recallops.domain.signals import (
    CAUSES,
    SIGNALS,
    CauseDef,
    SignalDef,
    cause_name,
    keyword_overlap,
    tokenize,
)
from recallops.memory.models import MemoryItem


@dataclass
class SignalHit:
    signal: SignalDef
    strength: float
    evidence: list[EvidenceRef] = field(default_factory=list)

    @property
    def id(self) -> str:
        return self.signal.id

    @property
    def label(self) -> str:
        return self.signal.label


@dataclass
class ScoredCause:
    cause: CauseDef
    support: float = 0.0
    contradiction: float = 0.0
    support_confidence: float = 0.0
    supporting: list[EvidenceRef] = field(default_factory=list)
    contradicting: list[EvidenceRef] = field(default_factory=list)
    signals: list[str] = field(default_factory=list)
    memory_prior: float = 0.0
    memory_links: list[MemoryLink] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def cause_id(self) -> str:
        return self.cause.id


@dataclass
class EvidenceBundle:
    """Everything the RCA engine is allowed to look at for the current incident."""

    service: str
    incident_id: str
    symptom: str = ""
    evidence: list[EvidenceRef] = field(default_factory=list)
    memories: list[MemoryItem] = field(default_factory=list)
    deploy_versions: list[str] = field(default_factory=list)
    recent_deploy_version: str | None = None
    recent_deploy_minutes: float | None = None
    service_criticality: str = "standard"
    customer_impact: str = "low"
    memory_mode: str = ""
    stage: str = ""

    # ------------------------------------------------------------------ factories
    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "EvidenceBundle":
        evidence = [EvidenceRef.model_validate(e) if not isinstance(e, EvidenceRef) else e for e in payload.get("evidence", [])]
        memories = [MemoryItem.model_validate(m) if not isinstance(m, MemoryItem) else m for m in payload.get("memories", [])]
        return cls(
            service=str(payload.get("service", "")),
            incident_id=str(payload.get("incident_id", "")),
            symptom=str(payload.get("symptom", "")),
            evidence=evidence,
            memories=memories,
            deploy_versions=[str(v) for v in payload.get("deploy_versions", [])],
            recent_deploy_version=payload.get("recent_deploy_version"),
            recent_deploy_minutes=payload.get("recent_deploy_minutes"),
            service_criticality=str(payload.get("service_criticality", "standard")),
            customer_impact=str(payload.get("customer_impact", "low")),
            memory_mode=str(payload.get("memory_mode", "")),
            stage=str(payload.get("stage", "")),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "incident_id": self.incident_id,
            "symptom": self.symptom,
            "evidence": [e.model_dump(mode="json") for e in self.evidence],
            "memories": [m.model_dump(mode="json") for m in self.memories],
            "deploy_versions": self.deploy_versions,
            "recent_deploy_version": self.recent_deploy_version,
            "recent_deploy_minutes": self.recent_deploy_minutes,
            "service_criticality": self.service_criticality,
            "customer_impact": self.customer_impact,
            "memory_mode": self.memory_mode,
            "stage": self.stage,
        }

    # ------------------------------------------------------------------ accessors
    def by_kind(self, kind: EvidenceKind) -> list[EvidenceRef]:
        return [e for e in self.evidence if e.kind == kind]

    def logs(self) -> list[EvidenceRef]:
        return self.by_kind(EvidenceKind.LOG)

    def metrics(self) -> list[EvidenceRef]:
        return self.by_kind(EvidenceKind.METRIC)

    def deployments(self) -> list[EvidenceRef]:
        return self.by_kind(EvidenceKind.DEPLOYMENT)

    def metric_value(self, name: str) -> float | None:
        for m in self.metrics():
            if str(m.raw.get("name", "")) == name:
                try:
                    return float(m.raw.get("value"))
                except (TypeError, ValueError):
                    return None
        return None

    def top_signal_labels(self, limit: int = 3) -> list[str]:
        hits = detect_signals(self.evidence)
        return [h.label for h in sorted(hits.values(), key=lambda h: (-h.strength, h.signal.id))[:limit]]

    def memory_summary(self) -> str:
        if not self.memories:
            return "no relevant organisational memories were recalled"
        failed = [m for m in self.memories if m.kind is MemoryKind.ACTION_OUTCOME and m.helped is False]
        return (
            f"{len(self.memories)} memories recalled"
            + (f", including {len(failed)} recorded failed action(s)" if failed else "")
        )

    def searchable_text(self, limit: int = 60) -> str:
        parts = [self.symptom]
        parts += [e.title for e in self.evidence[:limit]]
        parts += [e.detail for e in self.logs()[:12]]
        return " ".join(p for p in parts if p)


# --------------------------------------------------------------------------- signals


def detect_signals(evidence: Sequence[EvidenceRef]) -> dict[str, SignalHit]:
    hits: dict[str, SignalHit] = {}
    for signal in SIGNALS.values():
        strength, matched = signal.match(evidence)
        if strength > 0 and matched:
            dedup: list[EvidenceRef] = []
            seen: set[str] = set()
            for ev in matched:
                if ev.id not in seen:
                    seen.add(ev.id)
                    dedup.append(ev)
            hits[signal.id] = SignalHit(signal=signal, strength=round(strength, 3), evidence=dedup)
    return hits


# --------------------------------------------------------------------------- causes


def rule_based_votes(bundle: EvidenceBundle) -> dict[str, ScoredCause]:
    """Score every known cause against the current evidence."""
    evidence = bundle.evidence
    signals = detect_signals(evidence)
    results: dict[str, ScoredCause] = {}

    for cause_id, cause in CAUSES.items():
        scored = ScoredCause(cause=cause)
        total_weight = sum(cause.signals.values()) or 1.0
        support_raw = 0.0
        for signal_id, weight in cause.signals.items():
            hit = signals.get(signal_id)
            if not hit:
                continue
            support_raw += weight * hit.strength
            scored.signals.append(signal_id)
            for ev in hit.evidence:
                if ev not in scored.supporting:
                    scored.supporting.append(ev)
        scored.support = min(1.0, support_raw / total_weight) if scored.signals else 0.0

        for signal_id, weight in cause.anti_signals.items():
            hit = signals.get(signal_id)
            if not hit:
                continue
            scored.contradiction += min(1.0, weight * hit.strength)
            for ev in hit.evidence:
                if ev not in scored.contradicting:
                    scored.contradicting.append(ev)

        # A cause whose signals never fire is not a hypothesis at all.
        if not scored.signals:
            continue
        scored.support_confidence = round(min(0.97, max(0.02, scored.support * (1.0 - 0.5 * scored.contradiction))), 3)
        results[cause_id] = scored
    return results


# --------------------------------------------------------------------------- memory


def memory_prior_for_cause(cause: CauseDef, memories: Sequence[MemoryItem], *, service: str) -> tuple[float, list[MemoryLink]]:
    """How strongly does organisational memory support this cause right now?

    Precedent only: a memory about a past incident can raise or lower a prior and
    can contradict the current evidence, but it never *is* the current evidence.
    """
    if not memories:
        return 0.0, []
    prior = 0.0
    links: list[MemoryLink] = []
    for m in memories:
        relevance = 0.0
        if m.cause_id and m.cause_id == cause.id:
            relevance += 0.55
        text = f"{m.title} {m.content} {m.reusable_lesson}"
        relevance += 0.45 * keyword_overlap(text, cause.memory_keywords)
        if service and m.service == service:
            relevance += 0.15
        if m.kind is MemoryKind.SERVICE_PATTERN:
            relevance += 0.1
        relevance = max(0.0, min(1.0, relevance))
        if relevance <= 0.12:
            continue
        contribution = 0.0
        if m.kind is MemoryKind.INCIDENT_EPISODE and m.cause_id == cause.id:
            contribution = 0.5 * relevance
        elif m.kind is MemoryKind.ACTION_OUTCOME and m.helped is True and m.cause_id == cause.id:
            contribution = 0.3 * relevance
        elif m.kind in {MemoryKind.SERVICE_PATTERN, MemoryKind.RUNBOOK_LESSON, MemoryKind.POSTMORTEM_LESSON}:
            contribution = 0.22 * relevance
        elif m.kind is MemoryKind.ACTION_OUTCOME and m.helped is False:
            contribution = -0.18 * relevance  # this cause's family has burned us
        elif m.kind is MemoryKind.ENGINEER_CORRECTION:
            contribution = -0.2 * relevance
        prior += contribution
        links.append(
            MemoryLink(
                id=m.id,
                kind=m.kind,
                text=(m.reusable_lesson or m.content or m.title)[:400],
                source=m.source,
                score=round(m.score, 3),
                incident_id=m.incident_id or None,
                service=m.service or None,
                relevance="relevant" if relevance > 0.5 else "weakly_relevant",
                why=m.why or ("recorded precedent" if m.kind is not MemoryKind.ACTION_OUTCOME else "previous action outcome"),
                strategy_hits=list(m.strategy_hits),
            )
        )
    links.sort(key=lambda link: -link.score)
    return max(-1.0, min(1.0, prior)), links


def memory_contradicts_cause(cause: CauseDef, memories: Sequence[MemoryItem], *, service: str) -> list[MemoryItem]:
    out: list[MemoryItem] = []
    for m in memories:
        text = f"{m.title} {m.content} {m.reusable_lesson}".lower()
        mentions = keyword_overlap(text, cause.memory_keywords)
        contradicts = keyword_overlap(text, cause.contradicting_memory_keywords)
        if mentions > 0.25 and contradicts > 0.3 and (m.cause_id != cause.id):
            out.append(m)
    return out


def detect_conflicts(
    bundle: EvidenceBundle,
    memories: Sequence[MemoryItem],
    *,
    top_causes: Sequence[str] = (),
) -> list[MemoryConflict]:
    """Find places where recalled memory disagrees with current evidence."""
    conflicts: list[MemoryConflict] = []
    signals = detect_signals(bundle.evidence)
    q_tokens = tokenize(bundle.searchable_text())

    for m in memories:
        if m.kind not in {MemoryKind.INCIDENT_EPISODE, MemoryKind.SERVICE_PATTERN, MemoryKind.ACTION_OUTCOME}:
            continue
        m_tokens = tokenize(f"{m.title} {m.content} {m.reusable_lesson}")
        if not m_tokens:
            continue
        # What cause family does this memory talk about, and do we see it now?
        claimed: list[str] = [cid for cid, cd in CAUSES.items() if keyword_overlap(" ".join(m_tokens), cd.memory_keywords) > 0.3]
        if not claimed and m.cause_id:
            claimed = [m.cause_id]
        for cause_id in claimed[:2]:
            cause = CAUSES.get(cause_id)
            if cause is None:
                continue
            live = [sid for sid in cause.signals if sid in signals and signals[sid].strength >= 0.5]
            if live:
                continue  # memory and evidence agree
            overlap = keyword_overlap(" ".join(q_tokens), cause.memory_keywords)
            if overlap < 0.12 and not top_causes:
                continue
            current_evidence = [
                f"[{e.id}] {e.title}" for hit in (signals[s] for s in sorted(signals)) for e in hit.evidence[:1]
            ][:3]
            best_other = sorted(signals.values(), key=lambda h: -h.strength)[:1]
            other = best_other[0].label if best_other else "no dominant signal"
            conflicts.append(
                MemoryConflict(
                    id=f"conf-{m.id[:16]}-{cause_id}",
                    subject=cause_name(cause_id),
                    historical_claim=(m.reusable_lesson or m.content or m.title)[:300],
                    memory_ref=m.id,
                    current_evidence=current_evidence,
                    verdict="conflict",
                    resolution=(
                        f"Historical memory about {cause_name(cause_id).lower()} does not match the current incident: "
                        f"none of its signals are present. Current evidence points at {other.lower()}. "
                        "The memory was used as context only and did not drive the recommendation."
                    ),
                    severity="warning",
                )
            )
    return conflicts[:6]


# --------------------------------------------------------------------------- deploy correlation


def deploy_correlation(bundle: EvidenceBundle) -> tuple[float, str]:
    """How strongly a recent deploy is implicated. A lead, never a proof."""
    dep_evidence = bundle.deployments()
    if not dep_evidence:
        return 0.0, "No deployment evidence available."
    top = dep_evidence[0]
    minutes = float(top.raw.get("minutes_before", 9999) or 9999)
    declared = float(top.raw.get("correlation", 0) or 0)
    if minutes <= 15:
        time_score, time_note = 0.55, f"deployed {minutes:.0f} min before onset"
    elif minutes <= 60:
        time_score, time_note = 0.35, f"deployed {minutes:.0f} min before onset"
    else:
        time_score, time_note = 0.1, f"deployed {minutes:.0f} min before onset (outside the 60 min correlation window)"
    score = min(1.0, max(time_score, declared))
    return round(score, 3), (
        f"{top.raw.get('service', bundle.service)} {top.raw.get('version', '?')} {time_note}; "
        "correlation is a lead that must be confirmed by a diagnostic, not proof of causation."
    )


__all__ = [
    "EvidenceBundle",
    "ScoredCause",
    "SignalHit",
    "cause_name",
    "deploy_correlation",
    "detect_conflicts",
    "detect_signals",
    "memory_contradicts_cause",
    "memory_prior_for_cause",
    "rule_based_votes",
]
