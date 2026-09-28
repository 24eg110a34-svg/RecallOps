"""LLM provider selection. Environment driven, never hardcoded."""

from __future__ import annotations

from recallops.config import Settings, get_settings
from recallops.services.llm.base import LLMProvider
from recallops.services.llm.local_heuristic import LocalHeuristicProvider
from recallops.services.llm.openai_compatible import OpenAICompatibleProvider

_cached: LLMProvider | None = None


def build_llm_provider(settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    provider = (s.llm_provider or "local_heuristic").strip().lower()

    if provider in {"local", "heuristic", "local_heuristic", "offline", "none", "demo"}:
        return LocalHeuristicProvider(reason="LLM_PROVIDER=local_heuristic - deterministic rule engine, no API calls.")

    if provider in {"openai", "openai_compatible", "groq", "openrouter", "vllm", "ollama", "custom"}:
        if not s.llm_api_key and provider in {"openai", "openai_compatible"}:
            return LocalHeuristicProvider(
                reason="LLM_API_KEY is not set - falling back to the deterministic rule engine so the demo keeps working."
            )
        return OpenAICompatibleProvider(
            api_key=s.llm_api_key or "not-needed",
            model=s.llm_model,
            base_url=s.llm_base_url,
            timeout=s.llm_timeout,
            temperature=s.llm_temperature,
        )

    return LocalHeuristicProvider(reason=f"Unknown LLM_PROVIDER={provider!r}; using the deterministic rule engine.")


def get_llm_provider() -> LLMProvider:
    global _cached
    if _cached is None:
        _cached = build_llm_provider()
    return _cached


def reset_llm_provider() -> None:
    global _cached
    _cached = None


__all__ = ["LLMProvider", "LocalHeuristicProvider", "OpenAICompatibleProvider", "build_llm_provider", "get_llm_provider", "reset_llm_provider"]
