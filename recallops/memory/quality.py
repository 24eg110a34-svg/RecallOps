"""Memory quality control.

Most incident text is noise. A memory system that stores everything becomes
useless within a week, so candidate memories are screened before they are
retained. The screening is explicit and reported: every rejection carries a
reason the UI shows, so a reviewer can see what was *not* learned and why.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from recallops.domain.enums import Durability, MemoryKind
from recallops.memory.models import MemoryItem

_DURABLE_HINTS = (
    "root cause",
    "resolved",
    "resolution",
    "do not",
    "avoid",
    "should check",
    "check first",
    "always",
    "never",
    "leak",
    "saturat",
    "pool",
    "timeout",
    "regression",
    "pattern",
    "recurring",
    "mitigation",
    "reusable lesson",
    "low-value",
    "primary check",
    "confirmed",
    "caused by",
    "introduced by",
)

_EPISODIC_ONLY = (
    "engineer restarted",
    "someone looked at",
    "i opened the dashboard",
    "pager fired",
    "oncall acked",
    "we waited",
    "still investigating",
    "assigned to",
    "war room opened",
)

_GENERALIZABLE_KINDS = {
    MemoryKind.RUNBOOK_LESSON,
    MemoryKind.SERVICE_PATTERN,
    MemoryKind.POSTMORTEM_LESSON,
    MemoryKind.ENGINEER_CORRECTION,
}


@dataclass
class QualityVerdict:
    durability: Durability
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    fingerprint: str = ""

    @property
    def accepted(self) -> bool:
        return self.durability is Durability.DURABLE


def evaluate(item: MemoryItem) -> QualityVerdict:
    """Decide whether a candidate memory is durable organisational learning."""
    reasons: list[str] = []
    warnings: list[str] = []
    text = f"{item.title} {item.content} {item.lesson} {item.reusable_lesson}".lower()
    body_len = len(item.content.strip()) + len(item.reusable_lesson.strip())

    if body_len < 40:
        reasons.append("Content is too short to carry a reusable fact.")
    if not item.service and item.kind not in _GENERALIZABLE_KINDS:
        reasons.append("No service scope: an unscoped episodic fact cannot be reused.")
    if not item.incident_id and item.kind not in _GENERALIZABLE_KINDS:
        reasons.append("No incident reference: provenance is required for trust.")

    lowered = text
    for phrase in _EPISODIC_ONLY:
        if phrase in lowered and not any(h in lowered for h in ("lesson", "do not", "avoid", "root cause")):
            reasons.append(f"Ephemeral narration ('{phrase}') with no reusable lesson.")
            break

    has_lesson = any(h in lowered for h in _DURABLE_HINTS)
    if not has_lesson:
        reasons.append("No causal, preventive or procedural statement found (nothing to reuse).")

    if item.kind is MemoryKind.ACTION_OUTCOME:
        if item.helped is None and not item.reusable_lesson:
            warnings.append("Action outcome is inconclusive; stored as an experience fact, not a durable rule.")
        if item.helped is False and not (item.reusable_lesson or item.lesson):
            reasons.append("Failed action without a reusable lesson is not worth storing.")

    if item.kind is MemoryKind.INCIDENT_EPISODE and not (item.cause_id or item.meta.get("confirmed")):
        warnings.append("Episode stored without a confirmed root cause: treat as a lead, not a fact.")
    if item.kind is MemoryKind.ENGINEER_CORRECTION and not (item.lesson or item.content.strip()):
        reasons.append("Correction without a corrected cause carries no signal.")

    if len(item.content) > 6000:
        warnings.append("Content truncated for retention size limits.")

    if reasons:
        return QualityVerdict(Durability.REJECTED, reasons, warnings, item.fingerprint)
    return QualityVerdict(Durability.DURABLE, ["Carries a reusable causal, preventive or procedural fact."], warnings, item.fingerprint)


_CLEAN = re.compile(r"\s+")


def normalize_dedup_key(item: MemoryItem) -> str:
    return _CLEAN.sub(" ", f"{item.kind.value} {item.service} {item.cause_id} {item.title.lower()} {item.content.lower()}").strip()[:400]


__all__ = ["QualityVerdict", "evaluate", "normalize_dedup_key"]
