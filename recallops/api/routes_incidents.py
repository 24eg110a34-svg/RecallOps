"""Incident routes: create, read, stream, analyse, act, resolve, memory."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from recallops.agent.catalog import catalog_payload, risk_summary
from recallops.agent.orchestrator import ActionNotFound, IncidentNotFound, InvalidTransition
from recallops.api.deps import get_container
from recallops.api.schemas import (
    AnalyzeRequest,
    ApproveRequest,
    CreateIncidentRequest,
    ExecuteRequest,
    FeedbackRequest,
    RejectRequest,
    ResolveRequest,
    RetainRequest,
    WhatIfRequest,
)
from recallops.domain.enums import ActionStatus
from recallops.persistence import models as orm
from recallops.services.events import event_stream

router = APIRouter(prefix="/api", tags=["incidents"])


@router.post("/incidents", status_code=201)
def create_incident(payload: CreateIncidentRequest) -> dict[str, Any]:
    container = get_container()
    try:
        return container.orchestrator.create_incident(
            payload.scenario_id,
            incident_id=payload.incident_id,
            run_id=payload.run_id,
            memory_enabled=payload.memory_enabled,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/incidents")
def list_incidents(
    limit: int = Query(default=50, ge=1, le=200),
    state: str | None = None,
    service: str | None = None,
) -> dict[str, Any]:
    container = get_container()
    session = container.orchestrator.session()
    try:
        query = session.query(orm.Incident).order_by(orm.Incident.created_at.desc()).limit(limit)
        if state:
            query = query.filter(orm.Incident.state == state.upper())
        if service:
            query = query.filter(orm.Incident.service == service)
        rows = query.all()
        return {
            "count": len(rows),
            "incidents": [
                {
                    "id": r.id,
                    "scenario_id": r.scenario_id,
                    "service": r.service,
                    "title": r.title,
                    "severity": r.severity,
                    "state": r.state,
                    "symptom": r.symptom,
                    "root_cause": r.root_cause,
                    "root_cause_id": r.root_cause_id,
                    "step_count": r.step_count,
                    "memory_mode": r.memory_mode,
                    "memory_assisted": r.memory_assisted,
                    "detected_at": r.detected_at.isoformat() if r.detected_at else None,
                    "resolved_at": r.resolved_at.isoformat() if r.resolved_at else None,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rows
            ],
        }
    finally:
        session.close()


@router.get("/incidents/{incident_id}")
def get_incident(incident_id: str) -> dict[str, Any]:
    container = get_container()
    record = container.orchestrator.load_record(incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown incident '{incident_id}'")
    session = container.orchestrator.session()
    try:
        incident = container.orchestrator.get_incident(session, incident_id)
        scenario = container.orchestrator.scenario_for(incident)
        payload = record.to_dict()
        payload.update(
            {
                "severity_assessment": record.severity_assessment.model_dump(mode="json") if record.severity_assessment else None,
                "impact": record.impact.model_dump(mode="json") if record.impact else None,
                "deployments": record.deployments,
                "metrics": record.metrics,
                "logs": record.logs[-40:],
                "dependencies": record.dependencies,
                "feedback": record.feedback,
                "conflicts": record.conflicts,
                "recalled_memory_ids": record.recalled_memory_ids,
                "sim_metrics": record.sim_metrics,
                "memories": [m.model_dump(mode="json") for m in record.memories],
                "postmortem": container.orchestrator.get_postmortem(incident_id),
                "runbook": container.orchestrator.get_runbook_for_incident(incident_id),
                "scenario": scenario.summary() if scenario else None,
                "stage_id": record.stage_id,
                "timeline": [e.model_dump(mode="json") for e in record.timeline()],
            }
        )
        return payload
    finally:
        session.close()


@router.get("/incidents/{incident_id}/stream")
async def stream_incident(incident_id: str, replay: bool = Query(default=True)) -> StreamingResponse:
    container = get_container()
    if container.orchestrator.load_record(incident_id) is None:
        raise HTTPException(status_code=404, detail=f"unknown incident '{incident_id}'")
    return StreamingResponse(
        event_stream(incident_id, replay=replay),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


@router.post("/incidents/{incident_id}/analyze")
async def analyze_incident(incident_id: str, payload: AnalyzeRequest | None = None) -> dict[str, Any]:
    container = get_container()
    body = payload or AnalyzeRequest()
    try:
        return await container.orchestrator.analyze(incident_id, memory_enabled=body.memory_enabled, reason=body.reason)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except InvalidTransition as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/incidents/{incident_id}/advance")
async def advance_incident(incident_id: str, steps: int = Query(default=1, ge=1, le=10), analyze: bool = True) -> dict[str, Any]:
    container = get_container()
    try:
        result: dict[str, Any] = {}
        for _ in range(steps):
            result = await container.orchestrator.advance(incident_id, analyze=analyze)
        return result
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/incidents/{incident_id}/actions/{action_id}/approve")
def approve_action(incident_id: str, action_id: str, payload: ApproveRequest | None = None) -> dict[str, Any]:
    container = get_container()
    body = payload or ApproveRequest()
    try:
        return container.orchestrator.approve(incident_id, action_id, approved_by=body.approved_by, note=body.note)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/incidents/{incident_id}/actions/{action_id}/reject")
def reject_action(incident_id: str, action_id: str, payload: RejectRequest | None = None) -> dict[str, Any]:
    container = get_container()
    body = payload or RejectRequest()
    try:
        return container.orchestrator.reject(incident_id, action_id, rejected_by=body.rejected_by, reason=body.reason)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/incidents/{incident_id}/actions/{action_id}/execute")
async def execute_action(incident_id: str, action_id: str, payload: ExecuteRequest | None = None) -> dict[str, Any]:
    container = get_container()
    body = payload or ExecuteRequest()
    try:
        return await container.orchestrator.execute_action(
            incident_id, action_id, approved_by=body.approved_by, what_if_only=body.what_if_only
        )
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ActionNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/incidents/{incident_id}/what-if")
def what_if(incident_id: str, payload: WhatIfRequest) -> dict[str, Any]:
    container = get_container()
    try:
        return container.orchestrator.what_if(incident_id, payload.action)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/incidents/{incident_id}/resolve")
def resolve_incident(incident_id: str, payload: ResolveRequest | None = None) -> dict[str, Any]:
    container = get_container()
    body = payload or ResolveRequest()
    try:
        return container.orchestrator.resolve(
            incident_id, root_cause_id=body.root_cause_id, resolution=body.resolution, resolved_by=body.resolved_by
        )
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/incidents/{incident_id}/memories")
def incident_memories(incident_id: str, limit: int = Query(default=50, ge=1, le=200)) -> dict[str, Any]:
    container = get_container()
    record = container.orchestrator.load_record(incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown incident '{incident_id}'")
    return {
        "incident_id": incident_id,
        "memory_mode": record.memory_mode,
        "recalled": record.recalled_memory_ids,
        "retained": [m.model_dump(mode="json") for m in record.memories],
        "count": len(record.memories),
    }


@router.post("/incidents/{incident_id}/feedback")
def incident_feedback(incident_id: str, payload: FeedbackRequest) -> dict[str, Any]:
    container = get_container()
    try:
        return container.orchestrator.record_feedback(
            incident_id,
            target_type=payload.target_type,
            target_id=payload.target_id,
            verdict=payload.verdict,
            comment=payload.comment,
            corrected_cause=payload.corrected_cause,
            author=payload.author,
        )
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/incidents/{incident_id}/memories/retain")
def incident_retain(incident_id: str, payload: RetainRequest | None = None) -> dict[str, Any]:
    container = get_container()
    body = payload or RetainRequest()
    try:
        return container.orchestrator.retain_incident_memory(incident_id, include_diagnostics=body.include_diagnostics)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/incidents/{incident_id}/replay")
def replay_incident(incident_id: str) -> dict[str, Any]:
    container = get_container()
    try:
        return container.orchestrator.replay(incident_id)
    except IncidentNotFound as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/incidents/{incident_id}/postmortem")
def incident_postmortem(incident_id: str) -> dict[str, Any]:
    container = get_container()
    postmortem = container.orchestrator.get_postmortem(incident_id)
    if postmortem is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} has no postmortem yet")
    return postmortem


@router.get("/incidents/{incident_id}/runbook")
def incident_runbook(incident_id: str) -> dict[str, Any]:
    container = get_container()
    runbook = container.orchestrator.get_runbook_for_incident(incident_id)
    if runbook is None:
        raise HTTPException(status_code=404, detail=f"incident {incident_id} has no runbook yet")
    return runbook


@router.get("/incidents/{incident_id}/events")
def incident_events(incident_id: str) -> dict[str, Any]:
    container = get_container()
    record = container.orchestrator.load_record(incident_id)
    if record is None:
        raise HTTPException(status_code=404, detail=f"unknown incident '{incident_id}'")
    return {"incident_id": incident_id, "count": len(record.events), "events": [e.model_dump(mode="json") for e in record.timeline()]}


@router.get("/actions/catalog")
def action_catalog() -> dict[str, Any]:
    return {"count": len(catalog_payload()), "risk_summary": risk_summary(), "actions": catalog_payload()}


__all__ = ["router"]
