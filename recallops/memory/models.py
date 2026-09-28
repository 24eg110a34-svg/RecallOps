"""Memory layer data model.

A :class:`MemoryItem` is the single currency of the memory layer. It is what we
retain (natural language + structured metadata), what recall returns, what the
agent cites as precedent, and what the graph projects. Keeping one shape means
Hindsight, the local mirror and the demo fallback are interchangeable.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from recallops.domain.enums import Durability, MemoryKind, MemoryMode

# Human label for each active memory mode. Never claim the demo store is
# Hindsight: the UI renders exactly this text.
MEMORY_MODE_LABELS: dict[str, str] = {
    MemoryMode.HINDSIGHT.value: "Hindsight (real memory server)",
    MemoryMode.LOCAL_HINDSIGHT.value: "Local Hindsight mirror (Hindsight unreachable)",
    MemoryMode.DEMO_FALLBACK.value: "Demo memory fallback (deterministic, not Hindsight)",
    MemoryMode.DISABLED.value: "Memory disabled (comparison run)",
}


class MemoryItem(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: MemoryKind
    title: str
    content: str
    service: str = ""
    incident_id: str = ""
    cause_id: str = ""
    action_id: str | None = None
    action: str = ""
    outcome: str | None = None
    helped: bool | None = None
    expected_signal: str = ""
    actual_outcome: str = ""
    lesson: str = ""
    reusable_lesson: str = ""
    tags: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    durability: Durability = Durability.DURABLE
    rejected_reason: str = ""
    occurred_at: datetime | None = None
    source: MemoryMode = MemoryMode.LOCAL_HINDSIGHT
    external_id: str | None = None
    memory_type: Literal["world", "experience", "observation"] = "world"
    score: float = 0.0
    strategy_hits: list[str] = Field(default_factory=list)
    why: str = ""
    meta: dict[str, Any] = Field(default_factory=dict)

    # ------------------------------------------------------------------ helpers
    @property
    def fingerprint(self) -> str:
        base = f"{self.kind}|{self.service}|{self.incident_id}|{self.cause_id}|{self.title}|{self.content}"
        return hashlib.sha1(base.encode("utf-8")).hexdigest()[:16]

    def searchable_text(self) -> str:
        parts = [
            self.title,
            self.content,
            self.service,
            self.cause_id.replace("_", " "),
            self.action,
            self.lesson,
            self.reusable_lesson,
            self.actual_outcome,
            " ".join(self.tags),
            " ".join(self.entities),
        ]
        return " ".join(p for p in parts if p)

    def to_retain_content(self) -> str:
        """The natural-language text handed to the memory layer for extraction."""
        lines = [f"[{self.kind.value}] {self.title}"]
        if self.service:
            lines.append(f"Service: {self.service}.")
        if self.incident_id:
            lines.append(f"Incident: {self.incident_id}.")
        if self.cause_id:
            lines.append(f"Root cause identifier: {self.cause_id}.")
        lines.append(self.content.strip())
        if self.action:
            lines.append(f"Action attempted: {self.action}.")
        if self.expected_signal:
            lines.append(f"Expected signal: {self.expected_signal}.")
        if self.actual_outcome:
            lines.append(f"Actual outcome: {self.actual_outcome}.")
        if self.outcome:
            helped = {True: "helped", False: "did not help", None: "inconclusive"}[self.helped if self.helped is not None else None]
            lines.append(f"Verdict: the action {helped} ({self.outcome}).")
        if self.reusable_lesson:
            lines.append(f"Reusable lesson: {self.reusable_lesson}")
        if self.tags:
            lines.append("Tags: " + ", ".join(self.tags) + ".")
        return "\n".join(lines)

    def to_metadata(self) -> dict[str, str]:
        """Hindsight metadata is a flat string map - keep it flat and typed as text."""
        md: dict[str, str] = {
            "memory_kind": self.kind.value,
            "service": self.service,
            "incident_id": self.incident_id,
            "cause_id": self.cause_id,
            "durability": self.durability.value,
        }
        if self.action_id:
            md["action_id"] = self.action_id
        if self.outcome:
            md["outcome"] = self.outcome
        if self.helped is not None:
            md["helped"] = "true" if self.helped else "false"
        if self.occurred_at:
            md["occurred_at"] = self.occurred_at.isoformat()
        md["entities"] = ",".join(self.entities)
        return {k: v for k, v in md.items() if v != ""}

    def to_hindsight_item(self) -> dict[str, Any]:
        item: dict[str, Any] = {
            "content": self.to_retain_content(),
            "context": f"recallops {self.kind.value} for service {self.service or 'organisation'}",
            "metadata": self.to_metadata(),
            "tags": list(dict.fromkeys(self.tags[:8])) or ["recallops"],
            "entities": [{"text": e, "type": "CONCEPT"} for e in self.entities[:8]],
        }
        if self.occurred_at:
            item["timestamp"] = self.occurred_at
        return item

    @classmethod
    def from_recall(
        cls,
        *,
        text: str,
        external_id: str,
        metadata: dict[str, Any] | None,
        memory_type: str = "world",
        tags: list[str] | None = None,
        entities: list[str] | None = None,
        source: MemoryMode = MemoryMode.HINDSIGHT,
        occurred_at: datetime | None = None,
        score: float = 0.0,
        strategy_hits: list[str] | None = None,
    ) -> "MemoryItem":
        md = {k: (v if v is not None else "") for k, v in (metadata or {}).items()}
        kind_raw = str(md.get("memory_kind") or "incident_episode")
        try:
            kind = MemoryKind(kind_raw)
        except ValueError:
            kind = MemoryKind.INCIDENT_EPISODE
        return cls(
            id=f"hs-{external_id[:40]}",
            external_id=external_id,
            kind=kind,
            title=str(md.get("title") or text[:90]),
            content=text,
            service=str(md.get("service") or ""),
            incident_id=str(md.get("incident_id") or ""),
            cause_id=str(md.get("cause_id") or ""),
            action=str(md.get("action") or ""),
            action_id=str(md.get("action_id")) if md.get("action_id") else None,
            outcome=str(md.get("outcome")) if md.get("outcome") else None,
            helped=(str(md.get("helped")).lower() == "true") if md.get("helped") else None,
            durability=Durability(str(md.get("durability") or Durability.DURABLE.value)),
            memory_type=memory_type if memory_type in {"world", "experience", "observation"} else "world",
            tags=list(tags or []),
            entities=list(entities or []),
            source=source,
            occurred_at=occurred_at,
            score=score,
            strategy_hits=list(strategy_hits or []),
        )


class MemoryQuery(BaseModel):
    text: str
    service: str = ""
    incident_id: str = ""
    cause_ids: list[str] = Field(default_factory=list)
    kinds: list[MemoryKind] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    limit: int = 12
    budget: str = "mid"
    max_tokens: int = 2048
    query_timestamp: datetime | None = None
    min_score: float = 0.0
    exclude_incident_id: str | None = None


class MemoryRecallResult(BaseModel):
    items: list[MemoryItem] = Field(default_factory=list)
    mode: MemoryMode = MemoryMode.DISABLED
    provider: str = "disabled"
    degraded: bool = False
    degraded_reason: str = ""
    error: dict[str, Any] | None = None
    latency_ms: int = 0
    total_found: int = 0
    relevant_count: int = 0
    strategy_summary: dict[str, int] = Field(default_factory=dict)
    attempts: list[dict[str, Any]] = Field(default_factory=list)
    query: str = ""
    detail: str = ""

    @property
    def count(self) -> int:
        return len(self.items)

    def of_kind(self, kind: MemoryKind) -> list[MemoryItem]:
        return [i for i in self.items if i.kind == kind]

    def failed_actions(self) -> list[MemoryItem]:
        return [i for i in self.items if i.kind == MemoryKind.ACTION_OUTCOME and i.helped is False]

    def successful_actions(self) -> list[MemoryItem]:
        return [i for i in self.items if i.kind == MemoryKind.ACTION_OUTCOME and i.helped is True]


class MemoryWriteReceipt(BaseModel):
    written: int = 0
    rejected: int = 0
    rejected_items: list[dict[str, str]] = Field(default_factory=list)
    ids: list[str] = Field(default_factory=list)
    mode: MemoryMode = MemoryMode.DISABLED
    degraded: bool = False
    error: dict[str, Any] | None = None
    details: list[dict[str, Any]] = Field(default_factory=list)


class MemoryHealth(BaseModel):
    state: Literal["connected", "degraded", "unavailable", "disabled", "unknown"] = "unknown"
    mode: MemoryMode = MemoryMode.DISABLED
    mode_label: str = ""
    provider: str = "disabled"
    endpoint: str | None = None
    bank_id: str | None = None
    latency_ms: int | None = None
    detail: str = ""
    error: dict[str, Any] | None = None
    fallbacks: list[dict[str, Any]] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    circuit_breakers: dict[str, Any] = Field(default_factory=dict)
    hints: list[str] = Field(default_factory=list)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


__all__ = [
    "MEMORY_MODE_LABELS",
    "MemoryHealth",
    "MemoryItem",
    "MemoryQuery",
    "MemoryRecallResult",
    "MemoryWriteReceipt",
]
