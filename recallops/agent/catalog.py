"""Action catalog and risk classification.

Every action the agent can ever suggest is declared here with its risk, the tool
it uses and the causes it addresses. Risk is *derived* from properties (does it
change state, is it reversible, can it lose data) rather than hand-labelled, so
the safety gate has something to verify.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

from recallops.domain.enums import ActionType, RiskLevel

# Diagnostic tools are read-only by construction: they only read simulator state.


@dataclass(frozen=True)
class ActionDef:
    id: str
    description: str
    type: ActionType
    tool: str
    reason: str
    expected_signal: str
    causes: tuple[str, ...] = ()
    params: dict[str, Any] = field(default_factory=dict)
    reversible: bool = True
    production_impact: bool = False
    data_loss_risk: bool = False
    read_only: bool = False
    notes: str = ""
    tags: tuple[str, ...] = ()

    @property
    def risk(self) -> RiskLevel:
        return classify_risk(
            read_only=self.read_only,
            reversible=self.reversible,
            data_loss_risk=self.data_loss_risk,
            production_impact=self.production_impact,
        )

    @property
    def requires_confirmation(self) -> bool:
        return self.risk.is_state_changing


def classify_risk(
    *,
    read_only: bool,
    reversible: bool = True,
    data_loss_risk: bool = False,
    production_impact: bool = False,
) -> RiskLevel:
    """Derive the risk tier from action properties.

    - read-only            -> READ_ONLY   (safe to run in the simulator)
    - changes state        -> REVERSIBLE  (needs explicit human approval)
    - irreversible/lossy   -> HIGH_RISK   (advisory only, never auto-executed)
    """
    if read_only:
        return RiskLevel.READ_ONLY
    if data_loss_risk or not reversible:
        return RiskLevel.HIGH_RISK
    if production_impact:
        # A production-affecting but reversible action still needs a human.
        return RiskLevel.REVERSIBLE
    return RiskLevel.REVERSIBLE


# --------------------------------------------------------------------------- catalog

_DIAGNOSTICS: tuple[ActionDef, ...] = (
    ActionDef(
        id="inspect_error_rate",
        description="Inspect current error rate and blast radius",
        type=ActionType.DIAGNOSTIC,
        tool="get_metrics",
        reason="Establish how bad it is and which endpoints are affected before choosing a remediation.",
        expected_signal="error rate and affected endpoint list",
        read_only=True,
        params={"metrics": ["http_5xx_pct", "http_502_pct", "http_504_pct", "checkout_success_pct", "latency_p95_ms"]},
        tags=("triage",),
    ),
    ActionDef(
        id="inspect_recent_deploy",
        description="Inspect the last 24h of deployments for this service",
        type=ActionType.DIAGNOSTIC,
        tool="get_recent_deployments",
        reason="A deploy inside the incident window is the most common single cause and the cheapest to check.",
        expected_signal="list of recent versions with minutes-before-onset and change summary",
        read_only=True,
        params={"hours": 24},
        tags=("triage", "deploy"),
    ),
    ActionDef(
        id="inspect_service_health",
        description="Inspect service health and dependency status",
        type=ActionType.DIAGNOSTIC,
        tool="get_service_health",
        reason="Distinguishes an application fault from a dependency fault in one call.",
        expected_signal="per-dependency status and latency",
        read_only=True,
        params={},
        tags=("triage",),
    ),
    ActionDef(
        id="inspect_dependency_health",
        description="Inspect the dependency graph for degraded downstream services",
        type=ActionType.DIAGNOSTIC,
        tool="get_dependencies",
        reason="If a dependency is unhealthy the fault is outside the service and remediation must change.",
        expected_signal="dependency status, kind (internal/external) and criticality",
        read_only=True,
        params={},
        tags=("triage", "dependency"),
    ),
    ActionDef(
        id="inspect_redis_connections",
        description="Inspect Redis connection count vs configured max, per worker",
        type=ActionType.DIAGNOSTIC,
        tool="get_metrics",
        reason="The single most informative check when cache timeouts appear: acquired vs max and pool wait time.",
        expected_signal="redis_connections_active vs max_connections and pool acquisition wait time",
        read_only=True,
        params={"metrics": ["redis_connections_active", "redis_pool_wait_ms", "redis_cpu_utilization_pct"]},
        tags=("cache", "deep-dive"),
    ),
    ActionDef(
        id="inspect_connection_growth",
        description="Inspect whether connection count is growing monotonically (leak signature)",
        type=ActionType.DIAGNOSTIC,
        tool="get_metrics",
        reason="A monotonic rise after a deploy indicates a connection leak rather than an external load spike.",
        expected_signal="trend of connection count and open/unreturned connection counters",
        read_only=True,
        params={"metrics": ["redis_connections_active", "redis_pool_wait_ms"]},
        tags=("cache", "deep-dive"),
    ),
    ActionDef(
        id="inspect_db_connections",
        description="Inspect Postgres connection states and pool saturation",
        type=ActionType.DIAGNOSTIC,
        tool="get_metrics",
        reason="Separates database connection saturation from cache saturation before any remediation.",
        expected_signal="postgres_connections_active vs limit, db_pool_wait_ms, active/idle/waiting counts",
        read_only=True,
        params={"metrics": ["postgres_connections_active", "db_pool_wait_ms", "db_query_p95_ms", "postgres_cpu_utilization_pct"]},
        tags=("database", "deep-dive"),
    ),
    ActionDef(
        id="inspect_query_stats",
        description="Inspect top queries by total time and the query plan",
        type=ActionType.DIAGNOSTIC,
        tool="query_logs",
        reason="A single slow query can consume an entire connection pool; total_time is the highest-information metric.",
        expected_signal="top queries by total_time, avg duration and plan shape (seq scan / index)",
        read_only=True,
        params={"pattern": "slow query|duration:|seq scan|pg_stat|explain|n\\+1|queries per request", "limit": 20},
        tags=("database", "deep-dive"),
    ),
    ActionDef(
        id="inspect_cpu_profile",
        description="Inspect CPU utilisation, throttling and query volume",
        type=ActionType.DIAGNOSTIC,
        tool="get_metrics",
        reason="A CPU-bound incident needs query volume, not restarts: high CPU plus high queries-per-request means a code-path change.",
        expected_signal="cpu_utilization_pct, throttled_requests_pct, queries_per_request",
        read_only=True,
        params={"metrics": ["cpu_utilization_pct", "throttled_requests_pct", "queries_per_request", "db_queries_per_second", "postgres_cpu_utilization_pct"]},
        tags=("compute", "deep-dive"),
    ),
    ActionDef(
        id="inspect_retry_config",
        description="Inspect retry counts, timeouts and circuit breaker state",
        type=ActionType.DIAGNOSTIC,
        tool="query_logs",
        reason="Retry amplification converts a provider problem into a latency incident; breaker state shows whether we are still hammering it.",
        expected_signal="retry counts, timeout values and circuit breaker state",
        read_only=True,
        params={"pattern": "retry|circuit_breaker|timeout|backoff", "limit": 20},
        tags=("dependency",),
    ),
    ActionDef(
        id="inspect_redis_cpu",
        description="Inspect Redis server CPU and memory (the usual first guess)",
        type=ActionType.DIAGNOSTIC,
        tool="get_metrics",
        reason="Rules Redis-side saturation in or out. Note: healthy Redis CPU does NOT rule out client-side pool exhaustion.",
        expected_signal="redis_cpu_utilization_pct, redis memory, connected clients",
        read_only=True,
        params={"metrics": ["redis_cpu_utilization_pct", "redis_connections_active"]},
        tags=("cache", "triage"),
    ),
)

_REMEDIATIONS: tuple[ActionDef, ...] = (
    ActionDef(
        id="restart_api_pool",
        description="Restart the demo workers / pods of the affected service",
        type=ActionType.MITIGATION,
        tool="execute_demo_action",
        reason="Standard first reflex: clears leaked state and in-flight work.",
        expected_signal="error rate and resource utilisation fall immediately",
        causes=("redis_pool_exhaustion", "db_connection_saturation", "memory_pressure", "connection_leak_regression", "n_plus_one_query_cpu", "third_party_timeout"),
        reversible=True,
        production_impact=True,
        notes="Temporary mitigation only. It does not change the code or configuration that caused the failure.",
        tags=("mitigation", "restart"),
    ),
    ActionDef(
        id="rollback_recent_deploy",
        description="Roll back the most recent deployment of the affected service",
        type=ActionType.REMEDIATION,
        tool="execute_demo_action",
        reason="Removes the change correlated with onset; effective when the release introduced the failure condition.",
        expected_signal="error rate returns to baseline and the failing signature disappears",
        causes=("connection_leak_regression", "redis_pool_exhaustion", "n_plus_one_query_cpu", "slow_query_regression", "db_connection_saturation", "memory_pressure"),
        reversible=True,
        production_impact=True,
        notes="Reversible (can be re-deployed) but production-affecting: requires explicit human approval.",
        tags=("remediation", "deploy"),
    ),
    ActionDef(
        id="increase_redis_pool_size",
        description="Increase the per-worker Redis pool size and lower acquisition timeout",
        type=ActionType.CONFIGURATION,
        tool="execute_demo_action",
        reason="Buys connection headroom when the pool ceiling is the constraint.",
        expected_signal="pool wait time and acquisition failures fall; saturation postponed",
        causes=("redis_pool_exhaustion",),
        reversible=True,
        production_impact=True,
        notes="Capacity is finite. If a leak is still deployed, saturation returns later.",
        tags=("remediation", "config", "cache"),
    ),
    ActionDef(
        id="fix_connection_leak",
        description="Ship the connection-lifecycle fix (release the client on the error path)",
        type=ActionType.REMEDIATION,
        tool="execute_demo_action",
        reason="Addresses the actual defect rather than the symptom.",
        expected_signal="connection count returns to baseline and stays there under load",
        causes=("redis_pool_exhaustion", "connection_leak_regression"),
        reversible=True,
        production_impact=True,
        tags=("remediation", "code"),
    ),
    ActionDef(
        id="scale_db_pool",
        description="Increase the database connection pool ceiling (bounded by max_connections)",
        type=ActionType.CONFIGURATION,
        tool="execute_demo_action",
        reason="Helps only when the application pool, not the database, is the constraint.",
        expected_signal="pool wait time falls while database connections stay below 70% of max",
        causes=("db_connection_saturation",),
        reversible=True,
        production_impact=True,
        notes="Never set the application pool equal to the database max_connections.",
        tags=("remediation", "config", "database"),
    ),
    ActionDef(
        id="reduce_query_volume",
        description="Reduce query volume (disable optional joins / exports)",
        type=ActionType.CONFIGURATION,
        tool="execute_demo_action",
        reason="Lowers connection hold time by removing non-essential work from the request path.",
        expected_signal="query volume and pool wait fall within a minute",
        causes=("db_connection_saturation", "slow_query_regression", "n_plus_one_query_cpu"),
        reversible=True,
        production_impact=True,
        tags=("remediation", "config", "database"),
    ),
    ActionDef(
        id="kill_long_running_queries",
        description="Terminate long-running queries holding connections",
        type=ActionType.MITIGATION,
        tool="execute_demo_action",
        reason="Emergency lever: frees capacity immediately while the real fix is prepared.",
        expected_signal="active connections and queue depth fall sharply",
        causes=("db_connection_saturation", "slow_query_regression"),
        reversible=False,
        production_impact=True,
        data_loss_risk=True,
        notes="In-flight transactions are cancelled. Needs a follow-up fix or the incident returns.",
        tags=("mitigation", "database"),
    ),
    ActionDef(
        id="add_index",
        description="Create a supporting index on the hot table/column",
        type=ActionType.REMEDIATION,
        tool="execute_demo_action",
        reason="Removes a full scan that is holding connections for seconds per request.",
        expected_signal="query p95 collapses to baseline and pool utilisation falls",
        causes=("slow_query_regression", "db_connection_saturation", "n_plus_one_query_cpu"),
        reversible=True,
        production_impact=True,
        tags=("remediation", "database"),
    ),
    ActionDef(
        id="enable_prefetch",
        description="Re-enable eager loading / prefetch on the failing path",
        type=ActionType.REMEDIATION,
        tool="execute_demo_action",
        reason="Removes the per-item query pattern that pins CPU and database load.",
        expected_signal="queries-per-request returns to baseline and CPU falls",
        causes=("n_plus_one_query_cpu",),
        reversible=True,
        production_impact=True,
        tags=("remediation", "code"),
    ),
    ActionDef(
        id="scale_out_workers",
        description="Scale out more workers/replicas",
        type=ActionType.SCALING,
        tool="execute_demo_action",
        reason="Adds capacity when demand exceeds provisioned capacity.",
        expected_signal="per-pod saturation falls and latency improves",
        causes=("traffic_saturation", "memory_pressure"),
        reversible=True,
        production_impact=True,
        notes="Harmful when the constraint is a shared dependency (per-worker pools, a saturated database).",
        tags=("remediation", "scale"),
    ),
    ActionDef(
        id="enable_rate_limit",
        description="Enable rate limiting on the expensive endpoints",
        type=ActionType.MITIGATION,
        tool="execute_demo_action",
        reason="Protects a saturated backend from a traffic surge.",
        expected_signal="request rate and error rate fall as excess load is shed",
        causes=("traffic_saturation", "n_plus_one_query_cpu"),
        reversible=True,
        production_impact=True,
        tags=("mitigation", "capacity"),
    ),
    ActionDef(
        id="increase_memory_limit",
        description="Raise the container memory limit",
        type=ActionType.CONFIGURATION,
        tool="execute_demo_action",
        reason="Buys headroom when the process is genuinely memory-bound rather than leaky.",
        expected_signal="OOM kills stop and memory utilisation returns below 85%",
        causes=("memory_pressure",),
        reversible=True,
        production_impact=True,
        tags=("remediation", "config", "resources"),
    ),
    ActionDef(
        id="enable_degraded_mode",
        description="Enable degraded mode: skip the non-essential downstream call",
        type=ActionType.MITIGATION,
        tool="execute_demo_action",
        reason="Restores the user-facing path by moving a failing optional dependency off the request path.",
        expected_signal="user-facing latency and error rate return to baseline",
        causes=("third_party_timeout",),
        reversible=True,
        production_impact=True,
        notes="Accepts a business risk (deferred manual review) - it must be a conscious trade.",
        tags=("mitigation", "dependency"),
    ),
    ActionDef(
        id="isolate_dependency",
        description="Open the circuit breaker for the failing dependency (fail fast)",
        type=ActionType.MITIGATION,
        tool="execute_demo_action",
        reason="Fails fast into the fallback instead of waiting on multi-second provider timeouts plus retries.",
        expected_signal="requests complete through the fallback path and latency collapses",
        causes=("third_party_timeout", "network_connectivity"),
        reversible=True,
        production_impact=True,
        tags=("mitigation", "dependency"),
    ),
    ActionDef(
        id="increase_timeout",
        description="Increase the client timeout for the dependency",
        type=ActionType.CONFIGURATION,
        tool="execute_demo_action",
        reason="Only appropriate for a dependency that is slow rather than broken.",
        expected_signal="fewer timeouts, latency unchanged or worse",
        causes=("third_party_timeout", "network_connectivity"),
        reversible=True,
        production_impact=True,
        notes="Against a dead provider this converts fast failures into slow failures.",
        tags=("remediation", "config", "dependency"),
    ),
    ActionDef(
        id="failover_traffic",
        description="Shift traffic to the secondary region/cluster",
        type=ActionType.MITIGATION,
        tool="execute_demo_action",
        reason="Keeps serving when one region or path is impaired.",
        expected_signal="latency and error rate follow the secondary's health",
        causes=("network_connectivity", "traffic_saturation"),
        reversible=True,
        production_impact=True,
        tags=("mitigation", "network"),
    ),
    ActionDef(
        id="flush_cache",
        description="Flush the shared cache",
        type=ActionType.REMEDIATION,
        tool="execute_demo_action",
        reason="Only valid when stale or poisoned cache content is the fault.",
        expected_signal="cache miss rate spikes, database load rises",
        causes=(),
        reversible=False,
        production_impact=True,
        data_loss_risk=True,
        notes="Destroys cached state and can push load onto an already saturated datastore.",
        tags=("destructive",),
    ),
    ActionDef(
        id="failover_database",
        description="Fail over the primary database to the replica",
        type=ActionType.REMEDIATION,
        tool="execute_demo_action",
        reason="Reserved for genuine database unavailability.",
        expected_signal="connection errors stop after promotion",
        causes=(),
        reversible=False,
        production_impact=True,
        data_loss_risk=True,
        notes="Drops in-flight transactions and can lose replication lag. Advisory only in RecallOps.",
        tags=("destructive", "database"),
    ),
    ActionDef(
        id="drop_connections",
        description="Terminate all remaining database sessions",
        type=ActionType.REMEDIATION,
        tool="execute_demo_action",
        reason="Last-resort recovery from a wedged connection pool.",
        expected_signal="new connections succeed, in-flight work is lost",
        causes=(),
        reversible=False,
        production_impact=True,
        data_loss_risk=True,
        notes="Guarantees in-flight transaction loss. Advisory only in RecallOps.",
        tags=("destructive", "database"),
    ),
    ActionDef(
        id="broad_rollout",
        description="Roll the change out to all regions at once",
        type=ActionType.REMEDIATION,
        tool="execute_demo_action",
        reason="Speed over safety; only justified with a verified fix and a rollback plan.",
        expected_signal="n/a",
        causes=(),
        reversible=False,
        production_impact=True,
        data_loss_risk=True,
        notes="Maximises blast radius. Advisory only in RecallOps.",
        tags=("destructive", "deploy"),
    ),
    ActionDef(
        id="disable_circuit_breaker",
        description="Disable circuit breakers to force traffic through the failing dependency",
        type=ActionType.CONFIGURATION,
        tool="execute_demo_action",
        reason="Only correct when a breaker is masking an already-healthy dependency.",
        expected_signal="n/a",
        causes=(),
        reversible=False,
        production_impact=True,
        data_loss_risk=True,
        notes="Removes the protection that limits blast radius. Advisory only in RecallOps.",
        tags=("destructive", "dependency"),
    ),
)

ALL_ACTIONS: tuple[ActionDef, ...] = _DIAGNOSTICS + _REMEDIATIONS


@lru_cache(maxsize=1)
def action_catalog() -> dict[str, ActionDef]:
    return {a.id: a for a in ALL_ACTIONS}


def get_action(action_id: str) -> ActionDef | None:
    return action_catalog().get(action_id)


def diagnostic_actions() -> list[ActionDef]:
    return [a for a in ALL_ACTIONS if a.risk is RiskLevel.READ_ONLY]


def remediation_actions() -> list[ActionDef]:
    return [a for a in ALL_ACTIONS if a.risk is not RiskLevel.READ_ONLY]


def actions_for_cause(cause_id: str) -> list[ActionDef]:
    return [a for a in ALL_ACTIONS if cause_id in a.causes]


def describe_action(action_id: str) -> str:
    a = get_action(action_id)
    return a.description if a else action_id


def risk_summary() -> dict[str, int]:
    counts: dict[str, int] = {r.value: 0 for r in RiskLevel}
    for a in ALL_ACTIONS:
        counts[a.risk.value] += 1
    return counts


def catalog_payload() -> list[dict[str, Any]]:
    return [
        {
            "id": a.id,
            "description": a.description,
            "type": a.type.value,
            "risk": a.risk.value,
            "requires_confirmation": a.requires_confirmation,
            "tool": a.tool,
            "expected_signal": a.expected_signal,
            "causes": list(a.causes),
            "reversible": a.reversible,
            "production_impact": a.production_impact,
            "data_loss_risk": a.data_loss_risk,
            "notes": a.notes,
            "tags": list(a.tags),
        }
        for a in ALL_ACTIONS
    ]


def ids(values: Iterable[str]) -> list[str]:
    return [v for v in values]


__all__ = [
    "ALL_ACTIONS",
    "ActionDef",
    "action_catalog",
    "actions_for_cause",
    "catalog_payload",
    "classify_risk",
    "describe_action",
    "diagnostic_actions",
    "get_action",
    "remediation_actions",
    "risk_summary",
]
