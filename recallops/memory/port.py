"""The only interface the application uses to talk to durable memory.

Nothing above this line knows whether the answer came from a real Hindsight
server, a local mirror or the deterministic demo store - except
``MemoryRecallResult.mode``, which is deliberately explicit so the UI can be
honest about it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from recallops.memory.models import MemoryHealth, MemoryItem, MemoryQuery, MemoryRecallResult, MemoryWriteReceipt


class MemoryPort(ABC):
    """Durable organisational memory."""

    name: str = "memory"
    mode_label: str = "memory"

    @abstractmethod
    def recall(self, query: MemoryQuery, scope: str | None = None) -> MemoryRecallResult:
        """Retrieve memories relevant to ``query`` within an optional scope."""

    @abstractmethod
    def retain(self, items: list[MemoryItem], scope: str | None = None) -> MemoryWriteReceipt:
        """Durably store organisational learning."""

    @abstractmethod
    def health(self) -> MemoryHealth:
        """Provider health, fallback chain state and statistics."""

    # -- optional capabilities; adapters that cannot serve them return "unsupported"
    def graph(self, *, root_incident_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        return {"supported": False, "nodes": [], "edges": [], "detail": f"{self.name} does not expose a graph"}

    def list_memories(self, *, limit: int = 100, service: str | None = None, kind: str | None = None) -> dict[str, Any]:
        return {"supported": False, "items": [], "detail": f"{self.name} does not expose listing"}

    def stats(self) -> dict[str, Any]:
        return {}

    def reflect(self, query: str, *, budget: str = "low", context: str = "") -> str:
        """Optional: memory-grounded synthesis. Empty string when unsupported."""
        return ""

    def reset(self) -> None:
        """Clear adapter-local state (local mirror / demo store). Hindsight no-ops."""
        return None

    def close(self) -> None:
        return None


class MemoryDisabledAdapter(MemoryPort):
    """Memory OFF - used by the comparison runner. Never silently fakes results."""

    name = "disabled"
    mode_label = "Memory disabled (comparison run)"

    def recall(self, query: MemoryQuery, scope: str | None = None) -> MemoryRecallResult:
        from recallops.domain.enums import MemoryMode

        return MemoryRecallResult(
            items=[],
            mode=MemoryMode.DISABLED,
            provider="disabled",
            total_found=0,
            relevant_count=0,
            query=query.text,
            detail="memory_off",
        )

    def retain(self, items: list[MemoryItem], scope: str | None = None) -> MemoryWriteReceipt:
        from recallops.domain.enums import MemoryMode

        return MemoryWriteReceipt(written=0, mode=MemoryMode.DISABLED, rejected=len(items))

    def health(self) -> MemoryHealth:
        from recallops.domain.enums import MemoryMode

        return MemoryHealth(
            state="disabled",
            mode=MemoryMode.DISABLED,
            mode_label=self.mode_label,
            provider="disabled",
            detail="Memory is disabled for this run (comparison mode).",
        )


__all__ = ["MemoryDisabledAdapter", "MemoryPort"]
