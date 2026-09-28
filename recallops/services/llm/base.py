"""LLM provider abstraction.

The application never talks to a vendor SDK directly. Everything goes through
:class:`LLMProvider`, so the provider is swappable by environment variable and
the incident pipeline keeps working when no key is configured.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from recallops.domain.models import AnalystVerdict
from recallops.services.resilience import ProviderHealth


class LLMMessage(dict):
    """OpenAI-shaped message: {"role": ..., "content": ...}."""


class LLMProvider(ABC):
    name: str = "llm"
    model: str = "unknown"
    mode: str = "unknown"  # "api" | "local_heuristic"
    requires_api_key: bool = True

    @abstractmethod
    async def analyze_incident(self, payload: dict[str, Any]) -> AnalystVerdict: ...

    @abstractmethod
    async def write_text(self, system: str, user: str, *, max_tokens: int = 900) -> str: ...

    @abstractmethod
    async def health(self) -> ProviderHealth: ...

    def describe(self) -> dict[str, Any]:
        return {"provider": self.name, "model": self.model, "mode": self.mode, "requires_api_key": self.requires_api_key}
