"""Memory adapter selection and the fallback chain.

Resolution order (first one that works wins, and the UI is told which one it was):

1. ``HindsightAdapter``      - the real Hindsight server (Cloud or self-hosted)
2. ``LocalMemoryAdapter``    - local Hindsight mirror (same items, SQLite)
3. ``DemoMemoryAdapter``     - in-memory deterministic store (tests / offline)

There is no in-between: when Hindsight is configured but unreachable we degrade
to the local mirror and surface the reason, never silently pretend recall came
from Hindsight.
"""

from __future__ import annotations

import time
from typing import Any, Sequence

from recallops.config import Settings, get_settings
from recallops.domain.enums import MemoryMode
from recallops.memory.local_adapter import LocalMemoryAdapter
from recallops.memory.models import (
    MEMORY_MODE_LABELS,
    MemoryHealth,
    MemoryItem,
    MemoryQuery,
    MemoryRecallResult,
    MemoryWriteReceipt,
)
from recallops.memory.port import MemoryPort
from recallops.persistence.db import get_sessionmaker
from recallops.services.resilience import ErrorKind, ProviderError


class DemoMemoryAdapter(MemoryPort):
    """Process-local deterministic store. Used by tests and offline demos."""

    name = "demo_fallback"
    mode_label = MEMORY_MODE_LABELS[MemoryMode.DEMO_FALLBACK.value]

    def __init__(self) -> None:
        self._items: dict[str, MemoryItem] = {}

    def recall(self, query: MemoryQuery, scope: str | None = None) -> MemoryRecallResult:
        from recallops.memory.local_adapter import LocalMemoryAdapter as _Local  # scoring reuse

        items = list(self._items.values())
        if query.kinds:
            items = [i for i in items if i.kind in query.kinds]
        if query.exclude_incident_id:
            items = [i for i in items if i.incident_id != query.exclude_incident_id]
        for i in items:
            i.source = MemoryMode.DEMO_FALLBACK
        scored = LocalMemoryAdapter._score(self, items, query)  # type: ignore[arg-type]
        top = [it for _, it in scored[: query.limit] if it.score > 0]
        return MemoryRecallResult(
            items=top,
            mode=MemoryMode.DEMO_FALLBACK,
            provider=self.name,
            total_found=len(items),
            relevant_count=len(top),
            latency_ms=0,
            query=query.text,
            detail="in-process demo store",
        )

    def retain(self, items: list[MemoryItem], scope: str | None = None) -> MemoryWriteReceipt:
        for item in items:
            item.source = MemoryMode.DEMO_FALLBACK
            self._items[item.id] = item
        return MemoryWriteReceipt(written=len(items), ids=[i.id for i in items], mode=MemoryMode.DEMO_FALLBACK)

    def health(self) -> MemoryHealth:
        return MemoryHealth(
            state="connected",
            mode=MemoryMode.DEMO_FALLBACK,
            mode_label=self.mode_label,
            provider=self.name,
            detail=f"Deterministic demo memory store with {len(self._items)} items (NOT Hindsight).",
            stats={"total": len(self._items)},
        )

    def reset(self) -> None:
        self._items.clear()


class FallbackMemoryAdapter(MemoryPort):
    """Tries Hindsight, then the local mirror, then the demo store."""

    name = "fallback"

    def __init__(self, chain: Sequence[MemoryPort]) -> None:
        self._chain = list(chain)
        self.last_attempts: list[dict[str, Any]] = []
        self.active: MemoryPort = self._chain[-1] if self._chain else None  # type: ignore[assignment]

    # ------------------------------------------------------------------ helpers
    @property
    def local(self) -> LocalMemoryAdapter | None:
        for a in self._chain:
            if isinstance(a, LocalMemoryAdapter):
                return a
        return None

    def _try_chain(self, operation: str, fn_name: str, *args: Any, **kwargs: Any) -> tuple[Any, MemoryPort, list[dict[str, Any]]]:
        attempts: list[dict[str, Any]] = []
        last_error: ProviderError | None = None
        for adapter in self._chain:  # noqa: B007
            started = time.perf_counter()
            fn = getattr(adapter, fn_name)
            try:
                value = fn(*args, **kwargs)
            except ProviderError as exc:
                attempts.append({"adapter": adapter.name, "ok": False, "error": exc.kind.value, "message": exc.message})
                last_error = exc
                continue
            except Exception as exc:  # noqa: BLE001
                from recallops.security import scrub_exception

                message = scrub_exception(exc)
                attempts.append({"adapter": adapter.name, "ok": False, "error": "unexpected", "message": message[:200]})
                last_error = ProviderError(ErrorKind.UNKNOWN, message, provider=adapter.name, detail={"attempts": len(attempts)})
                continue
            attempts.append({"adapter": adapter.name, "ok": True, "latency_ms": int((time.perf_counter() - started) * 1000)})
            self.active = adapter
            return value, adapter, attempts
        raise last_error or ProviderError(ErrorKind.UNKNOWN, "no memory adapter available", provider="fallback")

    # ------------------------------------------------------------------ port API
    def recall(self, query: MemoryQuery, scope: str | None = None) -> MemoryRecallResult:
        try:
            result, adapter, attempts = self._try_chain("recall", "recall", query, scope)
        except ProviderError as exc:
            # Every layer failed: degrade visibly instead of breaking the incident.
            from recallops.domain.enums import MemoryMode as _Mode
            from recallops.memory.models import MemoryRecallResult as _Result

            return _Result(
                items=[],
                mode=_Mode.DEMO_FALLBACK,
                provider="fallback",
                degraded=True,
                degraded_reason=f"No memory layer available: {exc.message}",
                error=exc.to_dict(),
                query=query.text[:300],
                attempts=self.last_attempts,
            )
        self.last_attempts = attempts
        failures = [a for a in attempts if not a.get("ok")]
        if failures:
            first = failures[0]
            result.degraded = True
            if first.get("adapter") == "hindsight":
                result.degraded_reason = (
                    f"Hindsight unavailable ({first.get('error')}) - answered from the local memory mirror."
                )
            else:
                result.degraded_reason = (
                    f"Memory adapter '{first.get('adapter')}' failed ({first.get('error')}); "
                    "continuing with whatever is still available."
                )
            result.attempts = attempts
        return result

    def retain(self, items: list[MemoryItem], scope: str | None = None) -> MemoryWriteReceipt:
        try:
            receipt, adapter, attempts = self._try_chain("retain", "retain", items, scope)
        except ProviderError as exc:
            from recallops.domain.enums import MemoryMode as _Mode

            return MemoryWriteReceipt(
                written=0,
                rejected=len(items),
                mode=_Mode.DEMO_FALLBACK,
                degraded=True,
                error=exc.to_dict(),
            )
        self.last_attempts = attempts
        # Always keep the local mirror in sync: it backs the memory browser, the
        # graph and the demo comparison, and it is the next fallback target.
        local = self.local
        if local is not None and adapter is not local:
            try:
                mirror_items = [
                    item.model_copy(update={"source": MemoryMode.LOCAL_HINDSIGHT}) for item in items if item.id
                ]
                local.retain(mirror_items, scope)
            except Exception:  # noqa: BLE001 - mirror failure must not break the write
                pass
        receipt.degraded = adapter.name != "hindsight"
        if receipt.degraded:
            failed = next((a for a in attempts if a.get("adapter") == "hindsight" and not a.get("ok")), None)
            receipt.details.append(
                {
                    "status": "degraded",
                    "detail": f"retained in {adapter.name} instead of Hindsight ({failed.get('error') if failed else 'unknown'})",
                }
            )
        return receipt

    def health(self) -> MemoryHealth:
        entries: list[dict[str, Any]] = []
        health: MemoryHealth | None = None
        for adapter in self._chain:
            h = adapter.health()
            entries.append(
                {
                    "provider": adapter.name,
                    "state": h.state,
                    "mode": h.mode.value,
                    "detail": h.detail,
                    "latency_ms": h.latency_ms,
                    "error": h.error,
                }
            )
            if health is None:
                health = h
        assert health is not None
        health.fallbacks = entries
        breakers: dict[str, Any] = {}
        for adapter in self._chain:
            if hasattr(adapter, "breaker"):
                breakers[adapter.name] = adapter.breaker.to_dict()  # type: ignore[attr-defined]
        health.circuit_breakers = breakers
        return health

    def graph(self, *, root_incident_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        for adapter in self._chain:
            try:
                result = adapter.graph(root_incident_id=root_incident_id, limit=limit)
            except Exception:  # noqa: BLE001
                continue
            if result.get("nodes"):
                return result
        return {"supported": False, "nodes": [], "edges": [], "detail": "no memory graph available"}

    def list_memories(self, *, limit: int = 100, service: str | None = None, kind: str | None = None) -> dict[str, Any]:
        for adapter in self._chain:
            try:
                result = adapter.list_memories(limit=limit, service=service, kind=kind)
            except Exception:  # noqa: BLE001
                continue
            if result.get("items"):
                return result
        return {"supported": False, "items": [], "detail": "no memory listing available"}

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for adapter in self._chain:
            try:
                out[adapter.name] = adapter.stats()
            except Exception:  # noqa: BLE001
                continue
        return out

    def reflect(self, query: str, *, budget: str = "low", context: str = "") -> str:
        for adapter in self._chain:
            if hasattr(adapter, "reflect"):
                try:
                    text = adapter.reflect(query, budget=budget, context=context)  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    continue
                if text:
                    return text
        return ""

    def reset(self) -> None:
        for adapter in self._chain:
            try:
                adapter.reset()
            except Exception:  # noqa: BLE001
                continue


_cached: MemoryPort | None = None


def build_memory_adapter(settings: Settings | None = None, *, allow_demo_fallback: bool = True) -> MemoryPort:
    s = settings or get_settings()
    chain: list[MemoryPort] = []
    endpoint = s.hindsight_endpoint
    if endpoint and not s.hindsight_forced_off:
        from recallops.memory.hindsight_adapter import HindsightAdapter

        chain.append(
            HindsightAdapter(
                base_url=endpoint,
                bank_id=s.hindsight_bank_id,
                api_key=s.hindsight_api_key,
                timeout=s.hindsight_timeout,
                recall_budget=s.hindsight_recall_budget,
                recall_max_tokens=s.hindsight_recall_max_tokens,
                max_attempts=s.provider_max_attempts,
                breaker_threshold=s.provider_breaker_threshold,
                breaker_cooldown=s.provider_breaker_cooldown,
            )
        )
    chain.append(LocalMemoryAdapter(get_sessionmaker()))
    if allow_demo_fallback:
        chain.append(DemoMemoryAdapter())
    if len(chain) == 1:
        return chain[0]
    return FallbackMemoryAdapter(chain)


def get_memory_adapter() -> MemoryPort:
    global _cached
    if _cached is None:
        _cached = build_memory_adapter()
    return _cached


def set_memory_adapter(adapter: MemoryPort) -> None:
    """Injection point for tests and the comparison runner."""
    global _cached
    _cached = adapter


def reset_memory_adapter() -> None:
    global _cached
    if _cached is not None:
        try:
            _cached.close()
        except Exception:  # noqa: BLE001
            pass
    _cached = None


__all__ = [
    "DemoMemoryAdapter",
    "FallbackMemoryAdapter",
    "build_memory_adapter",
    "get_memory_adapter",
    "reset_memory_adapter",
    "set_memory_adapter",
]
