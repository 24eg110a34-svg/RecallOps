"""Insight routes: memory browser, graph, analytics, runbooks, postmortems, comparison, tools."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from recallops.api.deps import get_container
from recallops.api.schemas import ComparisonRunRequest
from recallops.memory.models import MemoryQuery
from recallops.persistence import models as orm
from recallops.services.analytics import AnalyticsService
from recallops.services.comparison import ComparisonService
from recallops.tools.base import ToolError

router = APIRouter(prefix="/api", tags=["insights"])


def _analytics() -> AnalyticsService:
    container = get_container()
    return AnalyticsService(
        session_factory=container.orchestrator.session,
        orchestrator=container.orchestrator,
        memory=container.memory,
    )


def _comparison() -> ComparisonService:
    container = get_container()
    return ComparisonService(orchestrator=container.orchestrator, memory=container.memory)


# --------------------------------------------------------------------------- memory


@router.get("/memory")
def memory_browser(
    limit: int = Query(default=100, ge=1, le=500),
    service: str | None = None,
    kind: str | None = None,
) -> dict[str, Any]:
    container = get_container()
    session = container.orchestrator.session()
    try:
        query = session.query(orm.MemoryRecord).order_by(orm.MemoryRecord.created_at.desc()).limit(limit)
        if service:
            query = query.filter(orm.MemoryRecord.service == service)
        if kind:
            query = query.filter(orm.MemoryRecord.kind == kind)
        rows = query.all()
        return {
            "count": len(rows),
            "mode": container.memory.health().mode.value,
            "items": [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "title": r.title,
                    "content": r.content,
                    "service": r.service,
                    "incident_id": r.incident_id,
                    "cause_id": r.cause_id,
                    "action_id": r.action_id,
                    "outcome": r.outcome,
                    "helped": (r.meta or {}).get("helped"),
                    "reusable_lesson": (r.meta or {}).get("reusable_lesson"),
                    "durability": r.durability,
                    "source": r.source,
                    "tags": r.tags,
                    "entities": r.entities,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }
    finally:
        session.close()


@router.get("/memory/search")
def memory_search(q: str = Query(min_length=2), limit: int = Query(default=12, ge=1, le=50)) -> dict[str, Any]:
    container = get_container()
    result = container.memory.recall(MemoryQuery(text=q, limit=limit, exclude_incident_id=None), scope="search")
    return result.model_dump(mode="json")


@router.get("/memory/graph")
def memory_graph(root_incident_id: str | None = None) -> dict[str, Any]:
    container = get_container()
    return container.memory.graph(root_incident_id=root_incident_id)


@router.get("/memory/quality")
def memory_quality() -> dict[str, Any]:
    return _analytics().memory_quality()


# --------------------------------------------------------------------------- insights


@router.get("/analytics")
def analytics() -> dict[str, Any]:
    service = _analytics()
    return {**service.overview(), "states": service.state_breakdown()}


@router.get("/analytics/patterns")
def analytics_patterns() -> dict[str, Any]:
    return {"patterns": _analytics().patterns()}


@router.get("/runbooks")
def runbooks() -> dict[str, Any]:
    items = _analytics().runbooks()
    return {"count": len(items), "runbooks": items}


@router.get("/postmortems")
def postmortems() -> dict[str, Any]:
    items = _analytics().postmortems()
    return {"count": len(items), "postmortems": items}


@router.get("/comparison")
def comparison_history() -> dict[str, Any]:
    return _comparison().history()


@router.get("/comparison/{scenario_id}")
def comparison(scenario_id: str) -> dict[str, Any]:
    result = _comparison().latest(scenario_id)
    if result is None:
        raise HTTPException(
            status_code=404,
            detail=f"no comparison run recorded for {scenario_id}; POST /api/comparison/{scenario_id}/run first",
        )
    return result


@router.post("/comparison/{scenario_id}/run")
def comparison_run(scenario_id: str, payload: ComparisonRunRequest | None = None) -> dict[str, Any]:
    body = payload or ComparisonRunRequest()
    try:
        return _comparison().run(scenario_id, persist=body.persist)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# --------------------------------------------------------------------------- tools


@router.get("/tools")
def tools() -> dict[str, Any]:
    container = get_container()
    return {"count": len(container.tools.names()), "tools": container.tools.describe()}


@router.post("/tools/{tool_name}/invoke")
def invoke_tool(tool_name: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    container = get_container()
    try:
        result = container.tools.call(tool_name, **(payload or {}))
    except ToolError as exc:
        raise HTTPException(status_code=400, detail=exc.to_dict()) from exc
    return result.model_dump(mode="json")


@router.get("/catalog")
def catalog() -> dict[str, Any]:
    from recallops.agent.catalog import catalog_payload, risk_summary

    return {"actions": catalog_payload(), "risk_summary": risk_summary()}


__all__ = ["router"]
