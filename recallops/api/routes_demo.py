"""Demo routes: deterministic reset, live simulation control, scripted demo run."""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import APIRouter, HTTPException

from recallops.api.deps import get_container
from recallops.api.schemas import DemoAdvanceRequest, DemoStartRequest, ResetRequest, RunDemoRequest
from recallops.persistence import models as orm
from recallops.persistence.db import reset_db
from recallops.services.demo import DemoService

router = APIRouter(prefix="/api/demo", tags=["demo"])


def _service() -> DemoService:
    container = get_container()
    return DemoService(
        orchestrator=container.orchestrator,
        memory=container.memory,
        session_factory=container.orchestrator.session,
    )


@router.get("/scenarios")
def scenarios() -> dict[str, Any]:
    container = get_container()
    return {"count": len(container.orchestrator.scenarios), "scenarios": container.orchestrator.scenarios.catalog()}


@router.get("/state")
def state() -> dict[str, Any]:
    return _service().state()


@router.post("/start")
async def start(payload: DemoStartRequest | None = None) -> dict[str, Any]:
    body = payload or DemoStartRequest()
    return await _service().start(body)


@router.post("/pause")
def pause() -> dict[str, Any]:
    return _service().pause()


@router.post("/resume")
async def resume() -> dict[str, Any]:
    return await _service().resume()


@router.post("/reset")
def reset(payload: ResetRequest | None = None) -> dict[str, Any]:
    body = payload or ResetRequest()
    container = get_container()
    from recallops.api.deps import build_container, set_container

    result = _service().reset(wipe_memory=body.wipe_memory)
    if body.recreate_incidents or body.seed:
        set_container(build_container())
    return result


@router.post("/scenarios/{scenario_id}/advance")
async def advance_scenario(scenario_id: str, payload: DemoAdvanceRequest | None = None) -> dict[str, Any]:
    body = payload or DemoAdvanceRequest()
    return await _service().advance_scenario(scenario_id, steps=body.steps, analyze=body.analyze)


@router.post("/run")
async def run_demo(payload: RunDemoRequest | None = None) -> dict[str, Any]:
    body = payload or RunDemoRequest()
    try:
        return await _service().run_scripted_demo(body.scenarios, run_comparison=body.run_comparison)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/seed")
def seed() -> dict[str, Any]:
    return _service().seed_all()


@router.get("/simulations")
def simulations() -> dict[str, Any]:
    session = _service().orchestrator.session()
    try:
        rows = session.query(orm.SimulationRun).order_by(orm.SimulationRun.started_at.desc()).limit(10).all()
        return {
            "count": len(rows),
            "runs": [
                {
                    "id": r.id,
                    "scenario_id": r.scenario_id,
                    "status": r.status,
                    "cursor": r.cursor,
                    "speed": r.speed,
                    "incident_id": r.incident_id,
                    "started_at": r.started_at.isoformat() if r.started_at else None,
                }
                for r in rows
            ],
        }
    finally:
        session.close()


__all__ = ["router"]
