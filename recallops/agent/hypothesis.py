"""Hypothesis generation, ranking and memory grounding.

The rule engine always produces the candidate set and the final ordering, so the
result is reproducible and explainable. An LLM, when configured, contributes
narrative and cross-checks - but every vote it casts is grounded against real
evidence ids and ungrounded votes are reported, not silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from recallops.agent.catalog import actions_for_cause
from recallops.agent.evidence import dedupe_evidence
from recallops.agent.rca import (
    EvidenceBundle,
    ScoredCause,
    deploy_correlation,
    detect_conflicts,
    detect_signals,
    memory_contradicts_cause,
    memory_prior_for_cause,
    rule_based_votes,
)
from recallops.domain.enums import MemoryKind
from recallops.domain.models import (
    AnalystVerdict,
    CauseHypothesis,
    EvidenceRef,
    GroundedAnalysis,
    MemoryConflict,
    MemoryLink,
)
from recallops.domain.scoring import blend_confidence
from recallops.domain.signals import CAUSES, cause_name, evidence_strength, keyword_overlap, tokenize
from recallops.memory.models import MemoryItem, MemoryRecallResult

MAX_HYPOTHESES = 5


@dataclass
class FeedbackHint:
    """Engineer feedback folded back into ranking."""

    target_id: str
    verdict: str
    comment: str = ""
    corrected_cause: str = ""


@dataclass
class HypothesisOutcome:
    hypotheses: list[CauseHypothesis] = field(default_factory=list)
    conflicts: list[MemoryConflict] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    analysis: GroundedAnalysis | None = None
    memory: MemoryRecallResult | None = None
    deploy_correlation: tuple[float, str] = (0.0, "")
    notes: list[str] = field(default_factory=list)

    @property
    def top(self) -> CauseHypothesis | None:
        return self.hypotheses[0] if self.hypotheses else None

    def by_cause(self, cause_id: str) -> CauseHypothesis | None:
        return next((h for h in self.hypotheses if h.cause_id == cause_id), None)

    @property
    def memory_assisted(self) -> bool:
        return any(h.memory_contribution > 0 for h in self.hypotheses)

    @property
    def memory_contribution(self) -> float:
        return max((h.memory_contribution for h in self.hypotheses), default=0.0)

    def memory_links(self) -> list[MemoryLink]:
        links: list[MemoryLink] = []
        for h in self.hypotheses:
            links.extend(h.memory_links)
        return links


def hypothesis_id(incident_id: str, cause_id: str) -> str:
    return f"{incident_id}::{cause_id}"


class HypothesisEngine:
    def __init__(self, *, llm: Any | None = None, max_hypotheses: int = MAX_HYPOTHESES) -> None:
        self.llm = llm
        self.max_hypotheses = max_hypotheses

    # ------------------------------------------------------------------ main
    async def run(
        self,
        bundle: EvidenceBundle,
        memory: MemoryRecallResult | None = None,
        *,
        feedback: Sequence[FeedbackHint] = (),
    ) -> HypothesisOutcome:
        memories = list(memory.items) if memory else list(bundle.memories)
        outcome = HypothesisOutcome(memory=memory)
        outcome.signals = detect_signals(bundle.evidence)
        outcome.deploy_correlation = deploy_correlation(bundle)

        votes = rule_based_votes(bundle)
        analysis = await self._llm_pass(bundle, memories)
        outcome.analysis = analysis
        llm_votes = self._ground_votes(analysis, bundle) if analysis and analysis.ok else {}

        rejected_causes: dict[str, str] = {}
        corrected_boost: dict[str, float] = {}
        for hint in feedback:
            if hint.target_id.startswith("hyp:") or "::" in hint.target_id:
                cause_id = hint.target_id.split("::", 1)[-1]
                if hint.verdict in {"incorrect", "wrong", "rejected"}:
                    rejected_causes[cause_id] = hint.comment or "rejected by the on-call engineer"
                elif hint.verdict in {"correct", "confirmed"}:
                    corrected_boost[cause_id] = 0.6
            if hint.corrected_cause:
                target = _match_cause(hint.corrected_cause)
                if target:
                    corrected_boost[target] = max(corrected_boost.get(target, 0.0), 0.5)

        hypotheses: list[CauseHypothesis] = []
        for cause_id, scored in votes.items():
            if cause_id in rejected_causes:
                continue
            hypothesis = self._build_hypothesis(bundle, memories, scored, llm_votes.get(cause_id))
            if corrected_boost.get(cause_id):
                hypothesis.confidence = min(0.97, hypothesis.confidence + corrected_boost[cause_id] * 0.25)
                hypothesis.confidence_basis += f"; confirmed by engineer feedback (+{corrected_boost[cause_id] * 0.25:.2f})"
                hypothesis.origin = "engineer"
            if corrected_boost and cause_id not in corrected_boost and hypothesis.confidence < 0.2:
                continue
            hypotheses.append(hypothesis)

        for cause_id, rejected_reason in rejected_causes.items():
            cause = CAUSES.get(cause_id)
            if cause is None:
                continue
            hypotheses.append(
                CauseHypothesis(
                    id=hypothesis_id(bundle.incident_id, cause_id),
                    cause=cause.cause,
                    category=cause.category,
                    confidence=0.02,
                    confidence_basis="rejected by the on-call engineer",
                    rationale=rejected_reason,
                    next_diagnostic=cause.next_diagnostic,
                    rejected=True,
                    rejected_reason=rejected_reason,
                    origin="engineer",
                )
            )

        order = {cid: i for i, cid in enumerate(CAUSES)}
        priority = {cid: cause.priority for cid, cause in CAUSES.items()}
        hypotheses.sort(
            key=lambda h: (
                h.rejected,
                -round(h.confidence, 2),
                -priority.get(h.cause_id, 0),
                order.get(h.cause_id, 99),
                h.cause,
            )
        )
        hypotheses = hypotheses[: self.max_hypotheses]

        for position, hypothesis in enumerate(hypotheses):
            hypothesis.rank = position
        outcome.hypotheses = hypotheses
        outcome.conflicts = detect_conflicts(bundle, memories, top_causes=[h.id.split("::")[-1] for h in hypotheses[:2]])

        if memory and memory.degraded:
            outcome.notes.append(memory.degraded_reason or "memory recall degraded")
        if analysis and not analysis.ok and analysis.error:
            outcome.notes.append(f"LLM reasoning unavailable ({analysis.error}); used the deterministic RCA engine only")
        return outcome

    # ------------------------------------------------------------------ pieces
    def _build_hypothesis(
        self,
        bundle: EvidenceBundle,
        memories: Sequence[MemoryItem],
        scored: ScoredCause,
        llm_vote: Any | None,
    ) -> CauseHypothesis:
        prior, links = memory_prior_for_cause(scored.cause, memories, service=bundle.service)
        contradicting_memories = memory_contradicts_cause(scored.cause, memories, service=bundle.service)
        effective_prior = prior - 0.15 * len(contradicting_memories)

        confidence = blend_confidence(
            support=scored.support,
            contradiction=min(1.0, scored.contradiction),
            memory_prior=effective_prior,
            evidence_count=len(scored.supporting),
        )

        supporting = sorted(dedupe_evidence(scored.supporting), key=lambda e: -evidence_strength(e))[:8]
        contradicting = sorted(dedupe_evidence(scored.contradicting), key=lambda e: -evidence_strength(e))[:6]

        if llm_vote is not None:
            from recallops.agent.evidence import find_evidence

            for eid in list(llm_vote.supporting)[:4]:
                ev = find_evidence(bundle.evidence, eid)
                if ev and ev not in supporting:
                    supporting.append(ev)
            for eid in list(llm_vote.contradicting)[:4]:
                ev = find_evidence(bundle.evidence, eid)
                if ev and ev not in contradicting:
                    contradicting.append(ev)
            confidence = 0.75 * confidence + 0.25 * float(llm_vote.confidence)
            origin = "llm"
        else:
            origin = "rule_engine"

        precedent = sorted({m.incident_id for m in memories if m.incident_id and m.cause_id == scored.cause.id and m.kind in {MemoryKind.INCIDENT_EPISODE, MemoryKind.SERVICE_PATTERN}})
        if not precedent:
            precedent = sorted({m.incident_id for m in memories if m.incident_id and scored.cause.id in f"{m.title} {m.content}".lower()})[:3]

        basis = (
            f"rule support {scored.support:.2f} from {len(scored.signals)} signal(s) "
            f"({', '.join(scored.signals)}); contradicting evidence {scored.contradiction:.2f}; "
            f"memory prior {effective_prior:+.2f}"
        )
        rationale_parts = [scored.cause.summary]
        if scored.contradiction:
            rationale_parts.append(
                "Contradicted by current evidence ("
                + ", ".join(e.title for e in contradicting[:2])
                + "), which lowers confidence rather than being ignored."
            )
        if prior > 0.15:
            rationale_parts.append(
                f"Organisational memory adds a prior of +{prior:.2f} - precedent, not proof; "
                "the current evidence still decides."
            )
        if contradicting_memories:
            rationale_parts.append(
                f"{len(contradicting_memories)} recalled memory/memories describe a different cause family for this symptom."
            )

        return CauseHypothesis(
            id=hypothesis_id(bundle.incident_id, scored.cause_id),
            cause=scored.cause.cause,
            category=scored.cause.category,
            confidence=round(confidence, 3),
            confidence_basis=basis,
            supporting=supporting,
            contradicting=contradicting,
            precedent=precedent,
            memory_links=links[:6],
            memory_contribution=round(max(0.0, effective_prior), 3),
            next_diagnostic=llm_vote.next_diagnostic if (llm_vote and llm_vote.next_diagnostic) else scored.cause.next_diagnostic,
            rationale=" ".join(p for p in rationale_parts if p),
            signals=scored.signals,
            origin=origin,  # type: ignore[arg-type]
        )

    # ------------------------------------------------------------------ llm
    async def _llm_pass(self, bundle: EvidenceBundle, memories: Sequence[MemoryItem]) -> GroundedAnalysis:
        if self.llm is None:
            return GroundedAnalysis(ok=False, provider="none", error="no llm provider configured")
        payload = bundle.to_payload()
        try:
            verdict: AnalystVerdict = await self.llm.analyze_incident(payload)
        except Exception as exc:  # noqa: BLE001 - LLM failures must never break analysis
            from recallops.security import redact_text

            return GroundedAnalysis(
                ok=False,
                provider=getattr(self.llm, "name", "llm"),
                model=getattr(self.llm, "model", ""),
                error=redact_text(str(exc))[:300],
            )
        grounded = self._ground_votes(verdict, bundle)
        return GroundedAnalysis(
            ok=bool(verdict.votes),
            provider=getattr(self.llm, "name", "llm"),
            model=getattr(self.llm, "model", ""),
            summary=verdict.summary[:600],
            votes=verdict.votes,
            grounded_votes=list(grounded.values()),
            rejected_votes=self._rejected_votes(verdict, bundle),
            used_memory=bool(memories),
        )

    def _ground_votes(self, verdict: AnalystVerdict | None, bundle: EvidenceBundle) -> dict[str, Any]:
        grounded: dict[str, Any] = {}
        if not verdict:
            return grounded
        known_ids = {e.id for e in bundle.evidence}
        for vote in verdict.votes:
            cause_id = _match_cause(vote.cause)
            if not cause_id:
                continue
            supporting = [e for e in vote.supporting if e in known_ids]
            contradicting = [e for e in vote.contradicting if e in known_ids]
            if not supporting and not vote.next_diagnostic:
                continue
            if cause_id in grounded:
                grounded[cause_id].supporting = list({*grounded[cause_id].supporting, *supporting})
                grounded[cause_id].contradicting = list({*grounded[cause_id].contradicting, *contradicting})
            else:
                grounded[cause_id] = type(vote)(**{**vote.model_dump(), "supporting": supporting, "contradicting": contradicting})
        return grounded

    def _rejected_votes(self, verdict: AnalystVerdict | None, bundle: EvidenceBundle) -> list[dict[str, str]]:
        if not verdict:
            return []
        known_ids = {e.id for e in bundle.evidence}
        out: list[dict[str, str]] = []
        grounded = self._ground_votes(verdict, bundle)
        for vote in verdict.votes:
            cause_id = _match_cause(vote.cause)
            if not cause_id:
                out.append({"cause": vote.cause[:80], "reason": "does not map to a known cause family"})
            elif cause_id not in grounded:
                out.append({"cause": vote.cause[:80], "reason": "cited no evidence from this incident (ungrounded)"})
            else:
                hallucinated = [e for e in vote.supporting if e not in known_ids]
                if hallucinated:
                    out.append(
                        {
                            "cause": vote.cause[:80],
                            "reason": f"cited unknown evidence ids: {', '.join(hallucinated[:3])}",
                        }
                    )
        return out[:6]


def _match_cause(text: str) -> str | None:
    """Map free-text cause names (LLM output) onto a known cause id."""
    if not text:
        return None
    cleaned = text.strip().lower()
    if cleaned in CAUSES:
        return cleaned
    tokens = tokenize(cleaned)
    if not tokens:
        return None
    best: tuple[float, str] | None = None
    for cause_id, cause in CAUSES.items():
        label_tokens = tokenize(cause.cause) | tokenize(cause.id.replace("_", " "))
        score = keyword_overlap(cleaned, [cause.cause, cause.id.replace("_", " ")])
        if tokens and label_tokens:
            score = max(score, len(tokens & label_tokens) / max(1, len(tokens | label_tokens)))
        if best is None or score > best[0]:
            best = (score, cause_id)
    if best and best[0] >= 0.34:
        return best[1]
    return None


def suggestion_for(hypothesis: CauseHypothesis, limit: int = 3) -> list[str]:
    cause_id = hypothesis.id.split("::", 1)[-1]
    return [a.id for a in actions_for_cause(cause_id)][:limit]


__all__ = [
    "FeedbackHint",
    "HypothesisEngine",
    "HypothesisOutcome",
    "MAX_HYPOTHESES",
    "cause_name",
    "hypothesis_id",
    "suggestion_for",
]
