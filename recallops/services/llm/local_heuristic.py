"""Deterministic offline reasoning provider.

Used when ``LLM_PROVIDER=local_heuristic`` or when an API key is missing. It is
*not* an LLM and never claims to be one - the UI labels it
"deterministic rule engine". It exists so the full incident loop (hypotheses,
confidence, postmortem prose) is reproducible and testable offline.

Its voting logic is the same RCA engine the agent uses, so switching providers
changes the narration and the ranking blend, never the grounding.
"""

from __future__ import annotations

from typing import Any

from recallops.domain.enums import EvidenceKind
from recallops.domain.models import AnalystVerdict, AnalystVote, EvidenceRef, ImpactAssessment
from recallops.services.llm.base import LLMProvider
from recallops.services.resilience import HealthState, ProviderHealth


class LocalHeuristicProvider(LLMProvider):
    name = "local_heuristic"
    model = "recallops-rule-engine-v1"
    mode = "local_heuristic"
    requires_api_key = False

    def __init__(self, reason: str = "") -> None:
        self.reason = reason or "Deterministic rule-based provider (no external model call)."

    async def analyze_incident(self, payload: dict[str, Any]) -> AnalystVerdict:
        from recallops.agent.rca import EvidenceBundle, rule_based_votes

        bundle = EvidenceBundle.from_payload(payload)
        votes: list[AnalystVote] = []
        for cause_id, scored in rule_based_votes(bundle).items():
            votes.append(
                AnalystVote(
                    cause=cause_id,
                    confidence=scored.support_confidence,
                    supporting=[e.id for e in scored.supporting][:6],
                    contradicting=[e.id for e in scored.contradicting][:6],
                    next_diagnostic=scored.cause.next_diagnostic,
                )
            )
        votes.sort(key=lambda v: v.confidence, reverse=True)
        summary = self._summary(bundle, votes)
        return AnalystVerdict(summary=summary, votes=votes[:5], raw_text="local_heuristic")

    def _summary(self, bundle: Any, votes: list[AnalystVote]) -> str:
        if not votes:
            return "No evidence matched a known failure pattern; escalating to deeper diagnostics."
        top = votes[0]
        signals = ", ".join(bundle.top_signal_labels(3)) or "no dominant signal"
        mem = bundle.memory_summary()
        tail = f" Memory: {mem}." if mem else " No organisational precedent was recalled."
        return (
            f"{len(bundle.evidence)} evidence items; dominant signals: {signals}. "
            f"Leading candidate: {top.cause.replace('_', ' ')} ({top.confidence:.0%}).{tail}"
        )

    async def write_text(self, system: str, user: str, *, max_tokens: int = 900) -> str:
        # The rule engine does not write prose; callers must use their own templates.
        return ""

    async def health(self) -> ProviderHealth:
        return ProviderHealth(
            name="llm",
            state=HealthState.CONNECTED,
            detail=self.reason,
            endpoint=None,
            latency_ms=0,
            extra={"provider": self.name, "model": self.model, "mode": self.mode, "api_key_required": False},
        )


def evidence_from_dicts(rows: list[dict[str, Any]]) -> list[EvidenceRef]:
    """Helper used by tests and by the comparison runner."""
    return [EvidenceRef.model_validate(row) for row in rows]


__all__ = ["EvidenceKind", "ImpactAssessment", "LocalHeuristicProvider", "evidence_from_dicts"]
