"""Real Hindsight adapter.

Uses the official ``hindsight-client`` SDK (``pip install hindsight-client``) and
only documented methods:

* ``Hindsight(base_url, api_key, timeout)``
* ``aget_version()``  - connectivity / API version
* ``aget_bank_config(bank_id)`` / ``acreate_bank(bank_id)`` - ensure the bank
* ``aretain_batch(bank_id, items=[...], document_id=...)`` - store learning
* ``arecall(bank_id, query, budget, max_tokens, types, tags)`` - multi-strategy recall
* ``alist_memories(bank_id, limit)`` - listing for the memory browser
* ``areflect(bank_id, query, budget)`` - memory-grounded synthesis
* ``client.entities.*`` - entity graph for the memory graph view

The SDK is an optional dependency. If it is missing, or the server is down, the
adapter raises a :class:`ProviderError` and the fallback chain in
``recallops.memory.factory`` takes over - it never pretends to be Hindsight.
"""

from __future__ import annotations

import asyncio
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Callable, Coroutine, TypeVar

from recallops.domain.enums import MemoryMode
from recallops.memory.models import (
    MEMORY_MODE_LABELS,
    MemoryHealth,
    MemoryItem,
    MemoryQuery,
    MemoryRecallResult,
    MemoryWriteReceipt,
)
from recallops.memory.port import MemoryPort
from recallops.security import redact_text
from recallops.services.resilience import (
    CircuitBreaker,
    ErrorKind,
    ProviderError,
    RetryPolicy,
    classify_exception,
    retry_async,
)

T = TypeVar("T")

_STRATEGY_MAP = {
    "semantic": "hindsight:semantic",
    "keyword": "hindsight:keyword",
    "graph": "hindsight:graph",
    "temporal": "hindsight:temporal",
    "reranker": "hindsight:reranker",
    "final": "hindsight:final-fusion",
}


def _run(coro: Coroutine[Any, Any, T]) -> T:
    """Run a coroutine from sync code, even inside a running event loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with ThreadPoolExecutor(max_workers=1) as pool:  # loop-free worker thread
        return pool.submit(asyncio.run, coro).result()


def _aware(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


class HindsightAdapter(MemoryPort):
    name = "hindsight"
    mode_label = MEMORY_MODE_LABELS[MemoryMode.HINDSIGHT.value]

    def __init__(
        self,
        *,
        base_url: str,
        bank_id: str,
        api_key: str | None = None,
        timeout: float = 6.0,
        recall_budget: str = "mid",
        recall_max_tokens: int = 2048,
        max_attempts: int = 3,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 20.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bank_id = bank_id
        self.api_key = api_key
        self.timeout = timeout
        self.recall_budget = recall_budget
        self.recall_max_tokens = recall_max_tokens
        self.policy = RetryPolicy(max_attempts=max_attempts, base_delay=0.4, max_delay=4.0)
        self.breaker = CircuitBreaker("hindsight", threshold=breaker_threshold, cooldown=breaker_cooldown)
        self._client: Any | None = None
        self._bank_ready = False
        self.last_error: ProviderError | None = None
        self.calls = {"retain": 0, "recall": 0, "health": 0, "failures": 0}

    # ------------------------------------------------------------------ client
    @property
    def client(self) -> Any:
        if self._client is None:
            try:
                from hindsight_client import Hindsight
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise ProviderError(
                    ErrorKind.BAD_REQUEST,
                    "hindsight-client is not installed. Run: pip install hindsight-client",
                    provider=self.name,
                    endpoint=self.base_url,
                ) from exc
            kwargs: dict[str, Any] = {"base_url": self.base_url, "timeout": self.timeout}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            self._client = Hindsight(**kwargs)
        return self._client

    def _ensure_bank(self) -> None:
        if self._bank_ready:
            return
        try:
            _run(self.client.aget_bank_config(bank_id=self.bank_id))
        except ProviderError:
            raise
        except Exception as exc:  # noqa: BLE001
            if classify_exception(exc) is ErrorKind.NOT_FOUND:
                try:
                    _run(
                        self.client.acreate_bank(
                            bank_id=self.bank_id,
                            name="RecallOps organisational memory",
                            mission=(
                                "Durable SRE learning: confirmed incident root causes, failed and successful "
                                "actions, service failure patterns and runbook lessons. Historical memory is "
                                "precedent, never proof of a current root cause."
                            ),
                        )
                    )
                except Exception as create_exc:  # noqa: BLE001
                    raise ProviderError(
                        classify_exception(create_exc),
                        redact_text(f"could not create memory bank '{self.bank_id}': {create_exc}"),
                        provider=self.name,
                        endpoint=self.base_url,
                    ) from create_exc
            elif classify_exception(exc) is ErrorKind.AUTH:
                raise ProviderError(
                    ErrorKind.AUTH,
                    "Hindsight rejected the API key (401). Check HINDSIGHT_API_KEY.",
                    provider=self.name,
                    endpoint=self.base_url,
                ) from exc
            else:
                raise ProviderError(
                    classify_exception(exc),
                    redact_text(f"bank '{self.bank_id}' is not reachable: {exc}"),
                    provider=self.name,
                    endpoint=self.base_url,
                ) from exc
        self._bank_ready = True

    # ------------------------------------------------------------------ recall
    def recall(self, query: MemoryQuery, scope: str | None = None) -> MemoryRecallResult:
        if not self.breaker.allow():
            raise ProviderError(
                ErrorKind.SERVER,
                "Hindsight circuit breaker is open",
                provider=self.name,
                endpoint=self.base_url,
                detail={"breaker": self.breaker.to_dict()},
            )
        started = time.perf_counter()
        attempts: list[dict[str, Any]] = []
        self._ensure_bank()
        text = self._build_query_text(query, scope)
        self.calls["recall"] += 1

        async def call() -> Any:
            return await self.client.arecall(
                bank_id=self.bank_id,
                query=text,
                types=["world", "experience", "observation"],
                budget=query.budget or self.recall_budget,
                max_tokens=query.max_tokens or self.recall_max_tokens,
                tags=list(dict.fromkeys(query.tags)) or None,
                include_chunks=False,
                query_timestamp=(query.query_timestamp or datetime.now(timezone.utc)).isoformat(),
            )

        try:
            response, log = _run_with_log(call, self.policy, self.name, self.base_url, attempts)
        except ProviderError as exc:
            self.breaker.record_failure(exc.kind.value, exc.message)
            self.calls["failures"] += 1
            self.last_error = exc
            raise
        self.breaker.record_success()
        self.last_error = None

        results = list(getattr(response, "results", None) or [])
        items: list[MemoryItem] = []
        for r in results[: query.limit]:
            scores = getattr(r, "scores", None) or {}
            score_map = {k: v for k, v in dict(scores).items() if isinstance(v, (int, float))}
            strategies = [_STRATEGY_MAP[k] for k, v in score_map.items() if v is not None and k in _STRATEGY_MAP]
            final = float(score_map.get("final") or score_map.get("reranker") or 0.0)
            item = MemoryItem.from_recall(
                text=getattr(r, "text", "") or "",
                external_id=str(getattr(r, "id", "")),
                metadata=dict(getattr(r, "metadata", None) or {}),
                memory_type=str(getattr(r, "type", "world") or "world"),
                tags=list(getattr(r, "tags", None) or []),
                entities=[str(e) for e in (getattr(r, "entities", None) or [])],
                source=MemoryMode.HINDSIGHT,
                occurred_at=_aware(getattr(r, "occurred_start", None)),
                score=round(final, 4),
                strategy_hits=strategies,
            )
            if query.exclude_incident_id and item.incident_id == query.exclude_incident_id:
                continue
            if query.service and item.service and item.service != query.service:
                # different service: keep, but say so
                item.why = f"different service ({item.service}) - precedent only"
            else:
                item.why = f"recalled from Hindsight bank '{self.bank_id}'"
            items.append(item)

        summary: dict[str, int] = {}
        for it in items:
            for s in it.strategy_hits:
                summary[s] = summary.get(s, 0) + 1
        return MemoryRecallResult(
            items=items,
            mode=MemoryMode.HINDSIGHT,
            provider=self.name,
            total_found=len(results),
            relevant_count=len(items),
            latency_ms=int((time.perf_counter() - started) * 1000),
            strategy_summary=summary,
            attempts=attempts or log,
            query=text,
            detail=f"hindsight bank {self.bank_id}",
        )

    def _build_query_text(self, query: MemoryQuery, scope: str | None = None) -> str:
        parts = [query.text.strip()]
        if query.service and query.service not in parts[0]:
            parts.append(f"service {query.service}")
        if query.cause_ids:
            parts.append("root cause families: " + ", ".join(c.replace("_", " ") for c in query.cause_ids))
        if scope and scope not in query.text:
            parts.append(f"scope: {scope}")
        return " ".join(p for p in parts if p)[:1200]

    # ------------------------------------------------------------------ retain
    def retain(self, items: list[MemoryItem], scope: str | None = None) -> MemoryWriteReceipt:
        if not self.breaker.allow():
            raise ProviderError(
                ErrorKind.SERVER,
                "Hindsight circuit breaker is open",
                provider=self.name,
                endpoint=self.base_url,
                detail={"breaker": self.breaker.to_dict()},
            )
        self._ensure_bank()
        self.calls["retain"] += 1
        written: list[str] = []
        details: list[dict[str, Any]] = []

        for item in items:
            payload = item.to_hindsight_item()
            document_id = f"recallops:{item.kind.value}:{item.incident_id or item.service or 'org'}:{item.id[:24]}"
            attempts: list[dict[str, Any]] = []
            try:
                _run_with_log(
                    lambda: self.client.aretain_batch(bank_id=self.bank_id, items=[payload], document_id=document_id),
                    self.policy,
                    self.name,
                    self.base_url,
                    attempts,
                )
            except ProviderError as exc:
                self.breaker.record_failure(exc.kind.value, exc.message)
                self.calls["failures"] += 1
                self.last_error = exc
                raise
            written.append(item.id)
            details.append({"id": item.id, "status": "written", "document_id": document_id, "attempts": attempts})

        self.breaker.record_success()
        return MemoryWriteReceipt(written=len(written), ids=written, mode=MemoryMode.HINDSIGHT, details=details)

    # ------------------------------------------------------------------ reflect
    def reflect(self, query: str, *, budget: str = "low", context: str = "") -> str:
        try:
            self._ensure_bank()
            answer = _run(self.client.areflect(bank_id=self.bank_id, query=query[:1200], budget=budget, context=context or None))
            return str(getattr(answer, "text", "") or "")
        except Exception as exc:  # noqa: BLE001 - reflect is optional
            kind = classify_exception(exc)
            self.last_error = exc if isinstance(exc, ProviderError) else ProviderError(kind, redact_text(str(exc)), provider=self.name)
            return ""

    # ------------------------------------------------------------------ health
    def health(self) -> MemoryHealth:
        started = time.perf_counter()
        self.calls["health"] += 1
        error: ProviderError | None = None
        detail = ""
        stats: dict[str, Any] = {}
        state = "connected"
        try:
            version = _run(self.client.aget_version())
            self._ensure_bank()
            detail = f"Hindsight API connected (version {getattr(version, 'api_version', 'unknown')})."
            try:
                memories = _run(self.client.alist_memories(bank_id=self.bank_id, limit=1))
                total = getattr(memories, "total", None)
                if total is None and isinstance(memories, dict):
                    total = memories.get("total")
                stats["bank_memories"] = total
            except Exception:  # noqa: BLE001 - listing is optional
                pass
        except ProviderError as exc:
            error, state = exc, "unavailable"
            detail = exc.message
        except Exception as exc:  # noqa: BLE001
            kind = classify_exception(exc)
            error = ProviderError(kind, redact_text(str(exc)), provider=self.name, endpoint=self.base_url)
            state, detail = "unavailable", f"{kind.value}: {error.message[:200]}"
        return MemoryHealth(
            state=state,  # type: ignore[arg-type]
            mode=MemoryMode.HINDSIGHT,
            mode_label=self.mode_label,
            provider=self.name,
            endpoint=self.base_url,
            bank_id=self.bank_id,
            latency_ms=int((time.perf_counter() - started) * 1000),
            detail=detail,
            error=error.to_dict() if error else None,
            stats=stats,
            circuit_breakers={"hindsight": self.breaker.to_dict()},
            hints=error.hints if error else [],
        )

    def list_memories(self, *, limit: int = 100, service: str | None = None, kind: str | None = None) -> dict[str, Any]:
        search = " ".join(x for x in (service, kind) if x) or None
        response = _run(self.client.alist_memories(bank_id=self.bank_id, search_query=search, limit=limit))
        units = getattr(response, "memory_units", None) or getattr(response, "units", None) or []
        return {
            "supported": True,
            "count": len(units),
            "items": [
                {
                    "id": str(getattr(u, "id", "")),
                    "text": str(getattr(u, "text", "") or getattr(u, "content", ""))[:600],
                    "type": str(getattr(u, "type", "world")),
                    "metadata": dict(getattr(u, "metadata", None) or {}),
                    "document_id": getattr(u, "document_id", None),
                }
                for u in units
            ],
            "detail": f"listed from Hindsight bank {self.bank_id}",
        }

    def graph(self, *, root_incident_id: str | None = None, limit: int = 200) -> dict[str, Any]:
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []
        try:
            entities = _run(self.client.entities.list_entities(bank_id=self.bank_id, limit=limit))
            for e in getattr(entities, "entities", None) or []:
                name = str(getattr(e, "name", None) or getattr(e, "id", ""))
                if not name:
                    continue
                nodes.append(
                    {
                        "id": f"entity:{name}",
                        "label": name,
                        "type": "hindsight_entity",
                        "count": int(getattr(e, "mention_count", 1) or 1),
                    }
                )
        except Exception as exc:  # noqa: BLE001
            return {
                "supported": False,
                "nodes": nodes,
                "edges": edges,
                "detail": f"Hindsight entity graph unavailable ({classify_exception(exc).value}); showing the local mirror instead.",
            }
        return {"supported": True, "nodes": nodes, "edges": edges, "detail": f"entity graph from Hindsight bank {self.bank_id}"}

    def stats(self) -> dict[str, Any]:
        return {
            "bank_id": self.bank_id,
            "endpoint": self.base_url,
            "calls": dict(self.calls),
            "breaker": self.breaker.to_dict(),
            "api_key_configured": bool(self.api_key),
        }

    def reset(self) -> None:
        """Demo reset. Deleting a real bank is destructive, so it is opt-in."""
        if not self._bank_ready:
            return
        try:
            _run(self.client.delete_bank(bank_id=self.bank_id))
        except Exception:  # noqa: BLE001
            pass
        self._bank_ready = False

    def close(self) -> None:
        client = self._client
        if client is not None and hasattr(client, "close"):
            try:
                client.close()
            except Exception:  # noqa: BLE001
                pass
        self._client = None


def _run_with_log(
    coro_factory: Callable[[], Coroutine[Any, Any, Any]],
    policy: RetryPolicy,
    provider: str,
    endpoint: str,
    log: list[dict[str, Any]],
) -> tuple[Any, list[dict[str, Any]]]:
    result, entries = _run(
        retry_async(
            coro_factory,
            policy=policy,
            provider=provider,
            endpoint=endpoint,
        )
    )
    log.extend(entries)
    return result, log


__all__ = ["HindsightAdapter"]
