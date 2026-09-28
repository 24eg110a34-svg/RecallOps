"""The nine RecallOps tools (plus two observability reads) wired to the app.

The handlers are bound by :func:`build_registry` to a *context* object that
supplies the session, the scenario library, the memory port and the orchestrator.
The LLM proposes tool calls by name; only this module turns them into data.
"""

from __future__ import annotations

from typing import Any

from recallops.agent.record import IncidentRecord
from recallops.tools.base import (
    ExecuteDemoActionParams,
    GetDependenciesParams,
    GetIncidentContextParams,
    GetMetricsParams,
    GetRecentDeploymentsParams,
    GetServiceHealthParams,
    QueryLogsParams,
    RecallMemoryParams,
    RecordActionParams,
    RecordFeedbackParams,
    RetainMemoryParams,
    ToolError,
    ToolRegistry,
    ToolResult,
)

TOOL_DESCRIPTIONS: dict[str, str] = {
    "get_incident_context": "Full normalised incident context: state, severity, evidence summary, hypotheses and actions.",
    "get_recent_deployments": "Deployment history for the affected service with minutes-before-onset and change summaries.",
    "query_logs": "Search the incident log stream by pattern/level (untrusted input is redacted and injection-hardened).",
    "get_service_health": "Service and dependency health for the affected service.",
    "get_metrics": "Current metric values with baseline and limit, optionally filtered by metric name.",
    "get_dependencies": "Dependency graph entries with status, kind, direction and criticality.",
    "recall_memory": "Recall organisational memory for this incident (Hindsight, or the documented fallback).",
    "record_action": "Record the lifecycle of an action: proposed / approved / rejected.",
    "execute_demo_action": "Execute an approved action in the deterministic simulator and return the outcome.",
    "record_feedback": "Record engineer feedback (correct/incorrect/helpful) which can become a correction memory.",
    "retain_memory": "Retain durable lessons for a resolved incident (episode, action outcomes, runbook, pattern).",
}


class ToolContext:
    """Everything the handlers need, injected once at app startup."""

    def __init__(self, *, orchestrator: Any, session_factory: Any, memory: Any, scenarios: Any) -> None:
        self.orchestrator = orchestrator
        self.session_factory = session_factory
        self.memory = memory
        self.scenarios = scenarios


def _load_record(ctx: ToolContext, incident_id: str) -> IncidentRecord:
    record = ctx.orchestrator.load_record(incident_id)
    if record is None:
        raise ToolError("get_incident_context", f"unknown incident '{incident_id}'", code="not_found")
    return record


# --------------------------------------------------------------------------- handlers


def _get_incident_context(ctx: ToolContext, params: GetIncidentContextParams) -> dict[str, Any]:
    record = _load_record(ctx, params.incident_id)
    return {
        "incident": {
            "id": record.id,
            "service": record.service,
            "severity": record.severity,
            "state": record.state,
            "symptom": record.symptom,
            "stage": record.stage_id,
            "root_cause": record.root_cause,
            "root_cause_id": record.root_cause_id,
            "memory_mode": record.memory_mode,
        },
        "severity_assessment": record.severity_assessment.model_dump(mode="json") if record.severity_assessment else None,
        "impact": record.impact.model_dump(mode="json") if record.impact else None,
        "evidence_count": len(record.evidence),
        "evidence": [e.model_dump(mode="json") for e in record.evidence[:25]],
        "hypotheses": [h.model_dump(mode="json") for h in record.ranked_hypotheses],
        "actions": [a.model_dump(mode="json") for a in record.actions],
        "metrics": record.metrics[-25:],
        "deployments": record.deployments,
        "dependencies": record.dependencies,
    }


def _get_recent_deployments(ctx: ToolContext, params: GetRecentDeploymentsParams) -> dict[str, Any]:
    record = _load_record(ctx, params.incident_id)
    cutoff = params.hours * 3600
    recent = [d for d in record.deployments if (d.get("minutes_before_incident") or 0) * 60 <= cutoff]
    return {
        "service": record.service,
        "window_hours": params.hours,
        "count": len(recent),
        "deployments": recent,
        "note": "A deploy inside the window is a lead to confirm, never proof of a cause.",
    }


def _query_logs(ctx: ToolContext, params: QueryLogsParams) -> dict[str, Any]:
    record = _load_record(ctx, params.incident_id)
    pattern = params.pattern.lower()
    rows = []
    for entry in record.logs:
        message = str(entry.get("message", ""))
        if pattern and pattern not in message.lower():
            continue
        if params.level and params.level.upper() not in str(entry.get("level", "")).upper():
            continue
        rows.append(entry)
        if len(rows) >= params.limit:
            break
    return {"incident_id": record.id, "pattern": params.pattern, "count": len(rows), "logs": rows}


def _get_service_health(ctx: ToolContext, params: GetServiceHealthParams) -> dict[str, Any]:
    record = _load_record(ctx, params.incident_id)
    data: dict[str, Any] = {
        "incident_id": record.id,
        "service": record.service,
        "state": record.state,
        "severity": record.severity,
        "metrics": {m["name"]: m["value"] for m in record.metrics[-20:] if m.get("name")},
    }
    if params.include_dependencies:
        data["dependencies"] = record.dependencies
    return data


def _get_metrics(ctx: ToolContext, params: GetMetricsParams) -> dict[str, Any]:
    record = _load_record(ctx, params.incident_id)
    rows = record.metrics
    if params.metrics:
        wanted = {m.lower() for m in params.metrics}
        rows = [m for m in rows if str(m.get("name", "")).lower() in wanted]
    rows = rows[-params.limit :]
    live = {name: value for name, value in (record.sim_metrics or {}).items()}
    return {
        "incident_id": record.id,
        "count": len(rows),
        "metrics": rows,
        "live_values": {m: live[m] for m in params.metrics if m in live} if params.metrics else live,
    }


def _get_dependencies(ctx: ToolContext, params: GetDependenciesParams) -> dict[str, Any]:
    record = _load_record(ctx, params.incident_id)
    return {
        "incident_id": record.id,
        "service": record.service,
        "count": len(record.dependencies),
        "dependencies": record.dependencies,
        "degraded": [d["name"] for d in record.dependencies if d.get("status") not in {"healthy", None}],
    }


def _recall_memory(ctx: ToolContext, params: RecallMemoryParams) -> dict[str, Any]:
    record = _load_record(ctx, params.incident_id)
    from recallops.agent.query import build_memory_query
    from recallops.agent.rca import EvidenceBundle
    from recallops.memory.models import MemoryQuery

    bundle = EvidenceBundle(
        service=record.service,
        incident_id=record.id,
        symptom=record.symptom,
        evidence=record.evidence,
    )
    base = build_memory_query(bundle, exclude_incident_id=record.id if params.exclude_current_incident else None)
    query = MemoryQuery(
        text=params.query or base.text,
        service=record.service,
        incident_id=record.id,
        cause_ids=base.cause_ids,
        limit=params.limit,
        budget=base.budget,
        exclude_incident_id=base.exclude_incident_id,
    )
    result = ctx.memory.recall(query, scope=record.service)
    return result.model_dump(mode="json")


def _record_action(ctx: ToolContext, params: RecordActionParams) -> dict[str, Any]:
    from recallops.domain.enums import ActionStatus

    try:
        status = ActionStatus(params.status)
    except ValueError as exc:
        raise ToolError("record_action", f"unknown action status '{params.status}'", code="invalid_status") from exc
    updated = ctx.orchestrator.record_action_state(
        params.incident_id,
        params.action_id,
        status=status,
        decided_by=params.decided_by or "agent",
        decision_reason=params.decision_reason,
    )
    return {"incident_id": params.incident_id, "action_id": params.action_id, "status": status.value, "updated": updated}


def _execute_demo_action(ctx: ToolContext, params: ExecuteDemoActionParams) -> dict[str, Any]:
    outcome = ctx.orchestrator.execute_action(
        params.incident_id,
        params.action_id,
        approved_by=params.approved_by,
        what_if_only=params.what_if_only,
    )
    return outcome


def _record_feedback(ctx: ToolContext, params: RecordFeedbackParams) -> dict[str, Any]:
    result = ctx.orchestrator.record_feedback(
        params.incident_id,
        target_type=params.target_type,
        target_id=params.target_id,
        verdict=params.verdict,
        comment=params.comment,
        corrected_cause=params.corrected_cause,
        author=params.author,
    )
    return result


def _retain_memory(ctx: ToolContext, params: RetainMemoryParams) -> dict[str, Any]:
    result = ctx.orchestrator.retain_incident_memory(params.incident_id, include_diagnostics=params.include_diagnostics)
    return result


# --------------------------------------------------------------------------- registry


def build_registry(ctx: ToolContext) -> ToolRegistry:
    registry = ToolRegistry()
    registry.register(
        "get_incident_context",
        description=TOOL_DESCRIPTIONS["get_incident_context"],
        params_model=GetIncidentContextParams,
        handler=lambda p: _get_incident_context(ctx, p),
        read_only=True,
        tags=("context",),
    )
    registry.register(
        "get_recent_deployments",
        description=TOOL_DESCRIPTIONS["get_recent_deployments"],
        params_model=GetRecentDeploymentsParams,
        handler=lambda p: _get_recent_deployments(ctx, p),
        read_only=True,
        tags=("deploy",),
    )
    registry.register(
        "query_logs",
        description=TOOL_DESCRIPTIONS["query_logs"],
        params_model=QueryLogsParams,
        handler=lambda p: _query_logs(ctx, p),
        read_only=True,
        tags=("logs",),
    )
    registry.register(
        "get_service_health",
        description=TOOL_DESCRIPTIONS["get_service_health"],
        params_model=GetServiceHealthParams,
        handler=lambda p: _get_service_health(ctx, p),
        read_only=True,
        tags=("health",),
    )
    registry.register(
        "get_metrics",
        description=TOOL_DESCRIPTIONS["get_metrics"],
        params_model=GetMetricsParams,
        handler=lambda p: _get_metrics(ctx, p),
        read_only=True,
        tags=("metrics",),
    )
    registry.register(
        "get_dependencies",
        description=TOOL_DESCRIPTIONS["get_dependencies"],
        params_model=GetDependenciesParams,
        handler=lambda p: _get_dependencies(ctx, p),
        read_only=True,
        tags=("dependency",),
    )
    registry.register(
        "recall_memory",
        description=TOOL_DESCRIPTIONS["recall_memory"],
        params_model=RecallMemoryParams,
        handler=lambda p: _recall_memory(ctx, p),
        read_only=True,
        tags=("memory",),
    )
    registry.register(
        "record_action",
        description=TOOL_DESCRIPTIONS["record_action"],
        params_model=RecordActionParams,
        handler=lambda p: _record_action(ctx, p),
        tags=("audit",),
    )
    registry.register(
        "execute_demo_action",
        description=TOOL_DESCRIPTIONS["execute_demo_action"],
        params_model=ExecuteDemoActionParams,
        handler=lambda p: _execute_demo_action(ctx, p),
        requires_approval=True,
        tags=("action", "simulator"),
    )
    registry.register(
        "record_feedback",
        description=TOOL_DESCRIPTIONS["record_feedback"],
        params_model=RecordFeedbackParams,
        handler=lambda p: _record_feedback(ctx, p),
        tags=("feedback",),
    )
    registry.register(
        "retain_memory",
        description=TOOL_DESCRIPTIONS["retain_memory"],
        params_model=RetainMemoryParams,
        handler=lambda p: _retain_memory(ctx, p),
        tags=("memory",),
    )
    return registry


__all__ = ["TOOL_DESCRIPTIONS", "ToolContext", "build_registry"]
