"""Memory layer: adapter behaviour, quality filtering, composition, graph."""

from __future__ import annotations

import pytest

from recallops.domain.enums import Durability, MemoryKind, MemoryMode
from recallops.memory.models import MemoryItem, MemoryQuery
from recallops.memory.port import MemoryDisabledAdapter
from recallops.memory.quality import evaluate


def _item(**kwargs) -> MemoryItem:
    base = dict(
        id="m1",
        kind=MemoryKind.INCIDENT_EPISODE,
        title="INC-A1: Redis pool exhaustion on payment-api",
        content=(
            "SEV-1 incident on payment-api. Symptoms: HTTP 503 rose from 0.4% to 31% after deploy v2.17.4. "
            "Confirmed root cause: a connection leak drained the per-worker Redis pool. "
            "Check first next time: per-worker Redis connection count versus max_connections."
        ),
        service="payment-api",
        incident_id="INC-A1",
        cause_id="redis_pool_exhaustion",
    )
    base.update(kwargs)
    return MemoryItem(**base)


def test_quality_accepts_a_durable_episode():
    verdict = evaluate(_item())
    assert verdict.accepted
    assert verdict.durability is Durability.DURABLE


def test_quality_rejects_ephemeral_narration():
    verdict = evaluate(
        _item(
            id="m2",
            kind=MemoryKind.ACTION_OUTCOME,
            title="Engineer restarted the API",
            content="Engineer restarted the API at 14:05 and watched the dashboard.",
            service="payment-api",
            incident_id="INC-A1",
            action="restart",
        )
    )
    assert not verdict.accepted
    assert verdict.reasons


def test_quality_warns_but_accepts_inconclusive_action():
    verdict = evaluate(
        _item(
            id="m3",
            kind=MemoryKind.ACTION_OUTCOME,
            title="Inspect Redis connections on payment-api",
            content="Read-only observation recorded; the incident state is unchanged, which is itself a signal.",
            service="payment-api",
            incident_id="INC-A1",
            action="inspect_redis_connections",
            helped=None,
            outcome="no_effect",
            reusable_lesson="Diagnostic 'Inspect Redis connections' produced no change and no decisive signal for this symptom; treat it as a low-value primary check.",
        )
    )
    assert verdict.accepted


def test_memory_query_serialisation_roundtrip():
    item = _item()
    payload = item.to_hindsight_item()
    assert "content" in payload and payload["context"].startswith("recallops")
    assert payload["metadata"]["cause_id"] == "redis_pool_exhaustion"
    assert "Redis" in payload["content"]


def test_disabled_adapter_returns_nothing():
    adapter = MemoryDisabledAdapter()
    result = adapter.recall(MemoryQuery(text="anything"))
    assert result.items == []
    assert result.mode is MemoryMode.DISABLED
    receipt = adapter.retain([_item()])
    assert receipt.written == 0


def test_local_adapter_recall_and_retain(clean_db):
    from recallops.memory.local_adapter import LocalMemoryAdapter
    from recallops.persistence.db import get_sessionmaker

    adapter = LocalMemoryAdapter(get_sessionmaker)
    receipt = adapter.retain([_item(), _item(id="m2", kind=MemoryKind.ACTION_OUTCOME, action_id="restart_api_pool", action="restart", helped=False, outcome="temporary_improvement", reusable_lesson="Restarting payment-api pods did not resolve Redis connection-pool exhaustion.")])
    assert receipt.written == 2
    assert adapter.retain([_item()]).written == 0  # idempotent

    result = adapter.recall(MemoryQuery(text="payment-api redis connection pool exhaustion restart pods", service="payment-api"))
    assert result.total_found == 2
    assert result.relevant_count >= 1
    top = result.items[0]
    assert top.score > 0
    assert top.strategy_hits

    failed = [i for i in result.items if i.kind is MemoryKind.ACTION_OUTCOME]
    assert failed, "the failed-action memory must be retrievable"
    assert failed[0].helped is False

    stats = adapter.stats()
    assert stats["total"] == 2
    assert stats["failed_action_memories"] == 1


def test_local_adapter_graph_projection(clean_db):
    from recallops.memory.local_adapter import LocalMemoryAdapter
    from recallops.persistence.db import get_sessionmaker

    adapter = LocalMemoryAdapter(get_sessionmaker)
    adapter.retain([_item()])
    graph = adapter.graph()
    types = {n["type"] for n in graph["nodes"]}
    assert {"incident", "service", "cause", "episode"} <= types
    assert graph["edges"]
    assert any(e["relation"] == "learned" for e in graph["edges"])


def test_exclude_incident_id_is_honoured(clean_db):
    from recallops.memory.local_adapter import LocalMemoryAdapter
    from recallops.persistence.db import get_sessionmaker

    adapter = LocalMemoryAdapter(get_sessionmaker)
    adapter.retain([_item()])
    result = adapter.recall(MemoryQuery(text="redis pool", service="payment-api", exclude_incident_id="INC-A1"))
    assert result.items == [] or all(i.incident_id != "INC-A1" for i in result.items)


def test_fallback_reports_degradation_instead_of_crashing(clean_db):
    from recallops.memory.factory import FallbackMemoryAdapter
    from recallops.memory.local_adapter import LocalMemoryAdapter
    from recallops.persistence.db import get_sessionmaker

    class Broken(LocalMemoryAdapter):
        name = "local_hindsight"

        def recall(self, query, scope=None):  # type: ignore[override]
            raise RuntimeError("simulated outage")

    chain = FallbackMemoryAdapter([Broken(get_sessionmaker)])
    result = chain.recall(MemoryQuery(text="redis"))
    assert result.degraded is True
    assert "simulated outage" in result.degraded_reason or "failed" in result.degraded_reason
    assert result.items == []
