"""Signal + root-cause knowledge base.

This is the deterministic grounding layer of the RCA engine. Every hypothesis
the agent produces must be explainable by evidence that matched a rule here, so
the ranking stays reproducible (and testable) whether or not an LLM is present.

Design: evidence -> ``Indicator`` matches -> ``Signal`` (a named fact such as
"redis pool saturated") -> ``CauseDef`` (a candidate root cause).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from recallops.domain.enums import EvidenceKind

# --------------------------------------------------------------------------- matching


@dataclass(frozen=True)
class Indicator:
    """A single, auditable evidence test."""

    id: str
    description: str
    kind: EvidenceKind | None = None
    contains: tuple[str, ...] = ()  # any-of (case-insensitive substring)
    requires_all: tuple[str, ...] = ()  # all-of substrings
    excludes: tuple[str, ...] = ()  # substring that invalidates the match
    field: str | None = None  # key inside EvidenceRef.raw
    where: tuple[tuple[str, Any], ...] = ()  # exact matches required inside raw
    value_gte: float | None = None
    value_lte: float | None = None
    ratio_gte: float | None = None  # value / limit
    weight: float = 1.0

    def matches(self, ev: Any) -> bool:
        kind = getattr(ev, "kind", None)
        if self.kind is not None and kind != self.kind:
            return False
        raw: dict[str, Any] = getattr(ev, "raw", None) or {}
        title = str(getattr(ev, "title", "") or "")
        detail = str(getattr(ev, "detail", "") or "")
        text = f"{title} {detail} {json.dumps(raw, default=str)}".lower()

        for bad in self.excludes:
            if bad.lower() in text:
                return False
        if self.contains:
            if not any(c.lower() in text for c in self.contains):
                return False
        for need in self.requires_all:
            if need.lower() not in text:
                return False
        for key, expected in self.where:
            if raw.get(key) != expected:
                return False

        if self.field is not None and raw.get(self.field) in (None, ""):
            return False
        if self.value_gte is not None or self.value_lte is not None or self.ratio_gte is not None:
            # Numeric predicates always read raw["value"] (and raw["limit"] for ratios);
            # ``field`` only selects which item we are talking about.
            value = raw.get("value")
            if value is None:
                return False
            try:
                num = float(value)
            except (TypeError, ValueError):
                return False
            if self.value_gte is not None and num < self.value_gte:
                return False
            if self.value_lte is not None and num > self.value_lte:
                return False
            if self.ratio_gte is not None:
                limit = raw.get("limit") or raw.get("max") or raw.get("threshold")
                try:
                    limit_num = float(limit)  # type: ignore[arg-type]
                except (TypeError, ValueError):
                    return False
                if limit_num <= 0 or num / limit_num < self.ratio_gte:
                    return False
        return True


@dataclass(frozen=True)
class SignalDef:
    id: str
    label: str
    category: str
    description: str
    supporting: tuple[Indicator, ...]
    contradictions: tuple[Indicator, ...] = ()
    weight: float = 1.0

    def match(self, evidence: Iterable[Any]) -> tuple[float, list[Any]]:
        """Return (0..1 strength, matched evidence).

        Strength is the *weighted* fraction of indicators that fired, so a signal
        whose decisive indicator (a leak signature, a saturated pool) is absent
        stays weak even when a weak corroborating indicator (a recent deploy)
        matched.
        """
        matched: list[Any] = []
        fired_weight = 0.0
        for ind in self.supporting:
            hits = [ev for ev in evidence if ind.matches(ev)]
            if hits:
                matched.extend(hits)
                fired_weight += ind.weight
        if not matched:
            return 0.0, []
        total_weight = sum(ind.weight for ind in self.supporting) or 1.0
        strength = min(1.0, fired_weight / total_weight)
        blocked = [ev for ind in self.contradictions for ev in evidence if ind.matches(ev)]
        if blocked:
            strength *= 0.35
            matched.extend(blocked)
        return strength, matched


# --------------------------------------------------------------------------- causes


@dataclass(frozen=True)
class CauseDef:
    id: str
    cause: str
    category: str
    summary: str
    signals: dict[str, float]
    anti_signals: dict[str, float] = field(default_factory=dict)
    next_diagnostic: str = ""
    diagnostics: tuple[str, ...] = ()
    remediations: tuple[str, ...] = ()
    memory_keywords: tuple[str, ...] = ()
    contradicting_memory_keywords: tuple[str, ...] = ()
    requires_deploy_correlation: bool = False
    # Tie-break within a family: when two causes explain the same evidence equally
    # well, the structural/mechanism cause is listed first (a saturated pool is
    # the mechanism; the slow query behind it is a contributing factor).
    priority: int = 0


# --------------------------------------------------------------------------- signals

POOL_SATURATION_INDICATORS = (
    Indicator(
        id="redis_conn_max",
        description="Active Redis connections equal the configured pool maximum",
        kind=EvidenceKind.METRIC,
        field="name",
        contains=("redis_connections_active", "redis_pool_active", "redis_client_connections"),
        ratio_gte=0.95,
        weight=2.0,
    ),
    Indicator(
        id="redis_timeout_logs",
        description="RedisTimeout / cache acquisition timeout in logs",
        contains=(
            "redistimeout",
            "redis_timeout",
            "redis read timeout",
            "cache acquisition timed out",
            "cache timeout",
            "client pool saturated",
        ),
        excludes=("database", "postgres", "sqlstate", "db pool", "orders-api.db", "max_connections"),
        weight=2.0,
    ),
    Indicator(
        id="pool_wait_metric",
        description="Redis connection pool wait time is climbing",
        kind=EvidenceKind.METRIC,
        contains=("pool_wait", "pool_acquire", "connection_wait"),
        excludes=("db_pool", "postgres", "pg_pool"),
        value_gte=100,
        weight=1.5,
    ),
)

REDIS_HEALTHY = (
    Indicator(
        id="redis_cpu_normal",
        description="Redis CPU is normal (a distractor, not a contradiction of pool saturation)",
        contains=("redis cpu", "redis_cpu"),
        value_lte=60,
        weight=0.25,
    ),
)

SIGNALS: dict[str, SignalDef] = {
    "redis_pool_saturation": SignalDef(
        id="redis_pool_saturation",
        label="Redis connection-pool saturation",
        category="cache",
        description="Active client connections to Redis sit at the configured maximum while acquisition waits climb.",
        supporting=POOL_SATURATION_INDICATORS,
        contradictions=(),
        weight=1.4,
    ),
    "cache_timeout_burst": SignalDef(
        id="cache_timeout_burst",
        label="Cache acquisition timeouts",
        category="cache",
        description="Service logs show cache/Redis acquisition timeouts rather than plain HTTP failures.",
        supporting=(
            Indicator(
                id="cache_timeout",
                description="Cache acquisition / RedisTimeout errors present",
                contains=(
                    "redistimeout",
                    "cache acquisition timed out",
                    "redis read timeout",
                    "cache timeout",
                    "redis_timeout",
                ),
                excludes=("database", "postgres", "sqlstate", "db pool", "max_connections"),
                weight=1.6,
            ),
            Indicator(
                id="checkout_5xx",
                description="Mixed 502/503 on cache-backed endpoints",
                contains=("502", "503", "bad gateway", "service unavailable"),
                weight=0.6,
            ),
        ),
        weight=1.2,
    ),
    "connection_leak_after_deploy": SignalDef(
        id="connection_leak_after_deploy",
        label="Connection leak correlated with recent deploy",
        category="regression",
        description="A deploy landed shortly before onset and the service leaks connections under the new code path.",
        supporting=(
            Indicator(
                id="recent_deploy",
                description="Deployment within the incident window",
                kind=EvidenceKind.DEPLOYMENT,
                contains=("deployed", "deployment", "release", "rollout"),
                weight=1.0,
            ),
            Indicator(
                id="leak_signature",
                description="Leak-shaped log signature (connections opened without close / leak)",
                contains=("connection leak", "leaked connection", "not returned to pool", "connection not released", "checkout_conn_leak"),
                weight=2.0,
            ),
            Indicator(
                id="monotonic_growth",
                description="Connection count grows monotonically after deploy",
                kind=EvidenceKind.METRIC,
                contains=("redis_connections_active",),
                value_gte=90,
                weight=1.2,
            ),
        ),
        weight=1.3,
    ),
    "db_connection_saturation": SignalDef(
        id="db_connection_saturation",
        label="PostgreSQL connection saturation",
        category="database",
        description="Application pool to PostgreSQL is at max, with waiters queued.",
        supporting=(
            Indicator(
                id="pg_conn_max",
                description="Postgres connections at configured maximum",
                kind=EvidenceKind.METRIC,
                contains=("postgres_connections_active", "pg_connections_active", "db_connections_active", "postgresql_connections"),
                ratio_gte=0.95,
                weight=2.0,
            ),
            Indicator(
                id="db_pool_timeout",
                description="Database pool timeout / remaining clients exhausted",
                contains=("remaining connection slots", "too many clients", "connection pool timeout", "db_pool_exhausted", "sqlstate 53300", "08006"),
                weight=1.8,
            ),
            Indicator(
                id="db_wait_metric",
                description="Pool wait time climbing for the database pool",
                kind=EvidenceKind.METRIC,
                contains=("db_pool_wait", "pg_pool_wait", "db_pool_acquire"),
                value_gte=100,
                weight=1.2,
            ),
        ),
        weight=1.4,
    ),
    "slow_query_regression": SignalDef(
        id="slow_query_regression",
        label="Slow query / query-plan regression",
        category="database",
        description="A specific query family got dramatically slower, saturating the database.",
        supporting=(
            Indicator(
                id="slow_query_log",
                description="Slow query log entries dominate",
                contains=("slow query", "duration: 4", "duration: 8", "seq scan", "query_planner", "explain analyze", "pg_slow_query"),
                weight=1.8,
            ),
            Indicator(
                id="query_time_up",
                description="Query duration metric sharply elevated",
                kind=EvidenceKind.METRIC,
                contains=("query_duration", "db_query_p95", "select_p95"),
                value_gte=2000,
                weight=1.5,
            ),
            Indicator(
                id="lock_wait",
                description="Lock waits / blocked queries reported",
                contains=("lock wait", "blocked by", "deadlock", "waiting on lock"),
                weight=1.0,
            ),
        ),
        weight=1.35,
    ),
    "n_plus_one_cpu": SignalDef(
        id="n_plus_one_cpu",
        label="N+1 query / CPU explosion",
        category="regression",
        description="A deploy turned one query into N per request, burning CPU.",
        supporting=(
            Indicator(
                id="cpu_saturation",
                description="Application CPU pinned near 100%",
                kind=EvidenceKind.METRIC,
                contains=("cpu_utilization", "process_cpu", "cpu_percent"),
                value_gte=90,
                weight=1.6,
            ),
            Indicator(
                id="query_count_explosion",
                description="Queries-per-request exploded",
                kind=EvidenceKind.METRIC,
                contains=("queries_per_request", "db_queries_per_second", "query_count"),
                value_gte=400,
                weight=1.8,
            ),
            Indicator(
                id="n_plus_one_log",
                description="Logs show N+1 shaped repeats / ORM lazy load",
                contains=("n+1", "lazy load", "select performed", "per-item query", "orm_n_plus_one", "cart_items_query"),
                weight=2.0,
            ),
            Indicator(
                id="deploy_regression",
                description="Recent deploy flagged as risky / changed ORM behaviour",
                kind=EvidenceKind.DEPLOYMENT,
                contains=("v2.3", "v2.4", "orm", "serializ", "prefetch"),
                weight=0.6,
            ),
        ),
        weight=1.3,
    ),
    "third_party_timeout": SignalDef(
        id="third_party_timeout",
        label="Downstream third-party dependency timeout",
        category="dependency",
        description="An external provider is timing out; our own dependencies look healthy.",
        supporting=(
            Indicator(
                id="external_timeout",
                description="External provider timing out",
                requires_all=("timeout",),
                contains=(
                    "upstream timeout",
                    "provider timeout",
                    "gateway timeout to external",
                    "external_api",
                    "fraud-scoring-eu",
                    "psp-acquirer-eu",
                    "stripe",
                    "sift",
                ),
                excludes=("healthy",),
                weight=1.8,
            ),
            Indicator(
                id="external_degraded",
                description="An external dependency is degraded or unhealthy",
                kind=EvidenceKind.DEPENDENCY,
                contains=("degraded", "unhealthy", "down"),
                where=(("kind", "external"),),
                weight=1.5,
            ),
            Indicator(
                id="external_slow",
                description="External dependency latency far above its own SLO",
                kind=EvidenceKind.DEPENDENCY,
                contains=("latency", "p95"),
                where=(("kind", "external"),),
                weight=0.9,
            ),
            Indicator(
                id="retries",
                description="Client retries amplifying the failure",
                contains=("retry_after", "circuit_breaker", "adding 8s", "retries "),
                weight=0.6,
            ),
        ),
        weight=1.3,
    ),
    "network_connectivity": SignalDef(
        id="network_connectivity",
        label="Network / DNS / connectivity fault",
        category="network",
        description="Name resolution or transport is failing rather than an application fault.",
        supporting=(
            Indicator(
                id="dns_fail",
                description="DNS resolution failures",
                contains=("name or service not known", "nxdomain", "dns", "could not resolve host", "eai_again", "getaddrinfo"),
                weight=2.0,
            ),
            Indicator(
                id="conn_refused",
                description="Connections refused / reset",
                contains=("connection refused", "econnrefused", "connection reset", "econnreset", "broken pipe"),
                weight=1.5,
            ),
        ),
        weight=1.2,
    ),
    "memory_pressure": SignalDef(
        id="memory_pressure",
        label="Memory pressure / OOM",
        category="resources",
        description="Container or process is being OOM-killed or thrashing.",
        supporting=(
            Indicator(
                id="oom",
                description="OOM kill / heap exhaustion",
                contains=("out of memory", "oomkilled", "oom-kill", "memoryerror", "heap out of memory", "container killed"),
                weight=2.2,
            ),
            Indicator(
                id="mem_high",
                description="Memory utilisation pinned high",
                kind=EvidenceKind.METRIC,
                contains=("memory_utilization", "mem_util", "rss"),
                value_gte=92,
                weight=1.2,
            ),
        ),
        weight=1.3,
    ),
    "traffic_saturation": SignalDef(
        id="traffic_saturation",
        label="Traffic surge / capacity overload",
        category="capacity",
        description="Request volume far above normal with no code or dependency change.",
        supporting=(
            Indicator(
                id="rps_spike",
                description="Request rate far above baseline",
                kind=EvidenceKind.METRIC,
                contains=("rps", "request_rate", "requests_per_second"),
                value_gte=4000,
                weight=1.4,
            ),
            Indicator(
                id="queue_depth",
                description="Queue depth exploding",
                kind=EvidenceKind.METRIC,
                contains=("queue_depth", "kafka_lag", "pending_requests"),
                value_gte=1000,
                weight=1.2,
            ),
            Indicator(
                id="no_deploy",
                description="No deployment in the incident window",
                kind=EvidenceKind.DEPLOYMENT,
                contains=("no deployment", "no_deploy", "none in window"),
                weight=0.8,
            ),
        ),
        weight=1.1,
    ),
    "healthy_redis": SignalDef(
        id="healthy_redis",
        label="Redis itself healthy",
        category="cache",
        description="Redis CPU/latency are nominal - argues against Redis itself being sick.",
        supporting=(
            Indicator(
                id="redis_norm",
                description="Redis CPU and latency normal",
                contains=("redis cpu", "redis_cpu", "redis latency"),
                value_lte=60,
                weight=1.0,
            ),
        ),
        weight=0.8,
    ),
    "healthy_postgres_cpu": SignalDef(
        id="healthy_postgres_cpu",
        label="Postgres CPU not saturated",
        category="database",
        description="DB CPU is normal while connections/waits rise - points at connection handling, not DB CPU.",
        supporting=(
            Indicator(
                id="pg_cpu_ok",
                description="Postgres CPU normal",
                contains=("postgres cpu", "postgres_cpu", "database cpu"),
                value_lte=60,
                weight=1.0,
            ),
        ),
        weight=0.7,
    ),
}


# --------------------------------------------------------------------------- causes

CAUSES: dict[str, CauseDef] = {
    "redis_pool_exhaustion": CauseDef(
        id="redis_pool_exhaustion",
        cause="Redis connection-pool exhaustion",
        category="cache",
        summary="Every worker holds the maximum number of Redis client connections, so cache acquisition blocks and the API returns 503.",
        signals={"redis_pool_saturation": 1.6, "cache_timeout_burst": 1.2, "connection_leak_after_deploy": 0.9},
        next_diagnostic="Inspect per-worker Redis connection count vs configured pool max, and whether connections are returned to the pool.",
        diagnostics=(
            "Count active Redis client connections per worker vs pool_max_connections",
            "Compare connection count against the deployment that introduced it",
            "Check pool acquisition wait time series for monotonic growth",
        ),
        remediations=("rollback_recent_deploy", "increase_redis_pool_size", "fix_connection_leak"),
        memory_keywords=("redis", "pool", "connection", "exhaust", "cachetimeout", "redistimeout", "checkout"),
    ),
    "connection_leak_regression": CauseDef(
        id="connection_leak_regression",
        cause="Connection leak introduced by the recent deployment",
        category="regression",
        summary="The newly deployed version opens Redis connections on a path that never returns them to the pool, so the pool drains over minutes.",
        signals={"connection_leak_after_deploy": 1.0},
        anti_signals={},
        next_diagnostic="Diff the deploy against the previous version for connection lifecycle changes and reproduce pool drain in staging.",
        diagnostics=(
            "Diff v_current vs v_previous for connection lifecycle changes",
            "Confirm the deploy timestamp precedes the first pool-saturation sample",
        ),
        remediations=("rollback_recent_deploy", "fix_connection_leak"),
        memory_keywords=("leak", "deploy", "rollback", "regression", "release", "version"),
    ),
    "db_connection_saturation": CauseDef(
        id="db_connection_saturation",
        cause="PostgreSQL connection saturation",
        category="database",
        summary="The application's Postgres pool is fully committed and waiters queue, so request threads block on connection acquisition.",
        signals={"db_connection_saturation": 1.6, "slow_query_regression": 0.4},
        next_diagnostic="Inspect Postgres connection counts by state (active/idle/waiting) and the application pool's max clients.",
        diagnostics=(
            "pg_stat_activity grouped by state and wait_event",
            "application pool max_clients vs max_connections",
        ),
        remediations=("restart_api_pool", "scale_db_pool", "reduce_query_volume"),
        memory_keywords=("postgres", "database", "db", "connection", "saturation", "max_connections", "slow query"),
        contradicting_memory_keywords=("redis",),
    ),
    "slow_query_regression": CauseDef(
        id="slow_query_regression",
        cause="Slow query regression on the database",
        category="database",
        summary="One query family became orders of magnitude slower, holding connections and starving the pool.",
        signals={"slow_query_regression": 1.7, "db_connection_saturation": 0.7},
        anti_signals={},
        next_diagnostic="Capture the top slow queries by total time and compare plans against the previous release.",
        diagnostics=("pg_stat_statements top total_time", "EXPLAIN (ANALYZE) the top offender", "compare with pre-deploy plan"),
        remediations=("add_index", "rollback_recent_deploy", "kill_long_running_queries"),
        memory_keywords=("slow", "query", "index", "plan", "seq scan", "latency"),
    ),
    "n_plus_one_query_cpu": CauseDef(
        id="n_plus_one_query_cpu",
        cause="N+1 query explosion from a bad deployment",
        category="regression",
        summary="A release changed serialization to per-item queries, multiplying database load and pinning CPU.",
        signals={"n_plus_one_cpu": 1.8},
        anti_signals={},
        next_diagnostic="Compare queries-per-request before and after the deploy and find the endpoint driving it.",
        diagnostics=("queries_per_request by route, pre/post deploy", "CPU hot methods in the profile"),
        remediations=("rollback_recent_deploy", "enable_prefetch"),
        memory_keywords=("n+1", "cpu", "deploy", "queries per request", "serialization"),
    ),
    "third_party_timeout": CauseDef(
        id="third_party_timeout",
        cause="Downstream third-party dependency timeout",
        category="dependency",
        summary="An external provider is slow or unavailable; our own datastore and cache are healthy.",
        signals={"third_party_timeout": 1.7},
        anti_signals={"redis_pool_saturation": 0.3, "db_connection_saturation": 0.3},
        next_diagnostic="Check the external provider's status and our per-provider timeout/circuit-breaker configuration.",
        diagnostics=("per-provider latency and error rate", "circuit breaker state", "provider status page"),
        remediations=("enable_degraded_mode", "increase_timeout", "isolate_dependency"),
        memory_keywords=("third-party", "external", "provider", "psp", "timeout", "dependency", "stripe", "fraud"),
        contradicting_memory_keywords=("redis", "postgres", "pool"),
    ),
    "network_connectivity": CauseDef(
        id="network_connectivity",
        cause="Network or DNS connectivity fault",
        category="network",
        summary="Transport-level failure between service and a dependency: DNS or refused connections.",
        signals={"network_connectivity": 1.7},
        anti_signals={},
        next_diagnostic="Resolve and connect to the dependency endpoint from inside the service network namespace.",
        diagnostics=("DNS resolution from the pod", "TCP connect test", "sidecar proxy logs"),
        remediations=("failover_traffic", "increase_timeout"),
        memory_keywords=("dns", "network", "connectivity", "connection refused", "timeout"),
    ),
    "memory_pressure": CauseDef(
        id="memory_pressure",
        cause="Memory pressure / OOM kill",
        category="resources",
        summary="The process exceeds its memory limit and is killed, shedding in-flight requests.",
        signals={"memory_pressure": 1.8},
        anti_signals={},
        next_diagnostic="Compare RSS and container limit, and look for OOM-kill events after the deploy.",
        diagnostics=("container OOM events", "heap profile", "limit vs request configuration"),
        remediations=("rollback_recent_deploy", "increase_memory_limit"),
        memory_keywords=("memory", "oom", "heap", "container"),
    ),
    "traffic_saturation": CauseDef(
        id="traffic_saturation",
        cause="Traffic surge beyond capacity",
        category="capacity",
        summary="Request volume far exceeds provisioned capacity; no code or dependency change involved.",
        signals={"traffic_saturation": 1.6},
        anti_signals={},
        next_diagnostic="Compare inbound RPS with the autoscaling ceiling and saturation points.",
        diagnostics=("RPS vs capacity plan", "autoscaler events", "per-pod saturation"),
        remediations=("scale_out_workers", "enable_rate_limit"),
        memory_keywords=("traffic", "rps", "capacity", "scale", "surge"),
    ),
}

# Causes that, when strongly signalled, explain "timeouts" on their own.
TIMEOUT_EXPLAINERS = ("redis_pool_exhaustion", "db_connection_saturation", "third_party_timeout", "network_connectivity", "slow_query_regression")


@lru_cache(maxsize=1)
def signal_index() -> dict[str, SignalDef]:
    return SIGNALS


@lru_cache(maxsize=1)
def cause_index() -> dict[str, CauseDef]:
    return CAUSES


def cause_name(cause_id: str) -> str:
    cd = CAUSES.get(cause_id)
    return cd.cause if cd else cause_id.replace("_", " ")


_TOKEN_RE = re.compile(r"[a-z0-9]+")


def tokenize(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def keyword_overlap(text: str, keywords: Iterable[str]) -> float:
    """Crude but explainable lexical similarity in [0, 1]."""
    tokens = tokenize(text)
    kw_list = [k for k in keywords if k]
    if not tokens or not kw_list:
        return 0.0
    hits = 0.0
    for kw in kw_list:
        kw_tokens = tokenize(kw)
        if not kw_tokens:
            continue
        hits += len(kw_tokens & tokens) / len(kw_tokens)
    return min(1.0, hits / len(kw_list))


def evidence_raw(ev: Any) -> dict[str, Any]:
    """Evidence raw payloads are dicts; be tolerant of anything else."""
    raw = getattr(ev, "raw", None)
    return raw if isinstance(raw, dict) else {}


def evidence_strength(ev: Any) -> float:
    """Weight an evidence item by how diagnostic it is (0.3 .. 1.0)."""
    kind = getattr(ev, "kind", None)
    base = {
        EvidenceKind.ALERT: 0.9,
        EvidenceKind.LOG: 0.8,
        EvidenceKind.METRIC: 0.85,
        EvidenceKind.DEPLOYMENT: 0.7,
        EvidenceKind.DEPENDENCY: 0.6,
        EvidenceKind.SERVICE_HEALTH: 0.7,
        EvidenceKind.SIMULATION: 0.75,
        EvidenceKind.OUTCOME: 0.9,
        EvidenceKind.ENGINEER: 0.95,
    }.get(kind, 0.5)
    if evidence_raw(ev).get("diagnostic"):
        base = min(1.0, base + 0.1)
    return base
