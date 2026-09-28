"""Demo service: reset, live simulation control, and the scripted demo run."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

from recallops.agent.orchestrator import IncidentOrchestrator
from recallops.memory.port import MemoryPort
from recallops.persistence import models as orm
from recallops.persistence.db import reset_db
from recallops.services.events import get_bus, publish_payload


class DemoService:
    def __init__(self, *, orchestrator: IncidentOrchestrator, memory: MemoryPort, session_factory: Any) -> None:
        self.orchestrator = orchestrator
        self.memory = memory
        self.session_factory = session_factory
        self._task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------ state
    def state(self) -> dict[str, Any]:
        session = self.orchestrator.session()
        try:
            runs = session.query(orm.SimulationRun).order_by(orm.SimulationRun.started_at.desc()).limit(5).all()
            incidents = session.query(orm.Incident).order_by(orm.Incident.created_at.desc()).limit(20).all()
            memories = session.query(orm.MemoryRecord).count()
            return {
                "simulation": {
                    "status": runs[0].status if runs else "idle",
                    "scenario_id": runs[0].scenario_id if runs else None,
                    "cursor": runs[0].cursor if runs else 0,
                    "speed": runs[0].speed if runs else 1.0,
                    "incident_id": runs[0].incident_id if runs else None,
                },
                "incidents": [
                    {
                        "id": i.id,
                        "service": i.service,
                        "severity": i.severity,
                        "state": i.state,
                        "root_cause_id": i.root_cause_id,
                        "steps": i.step_count,
                        "memory_mode": i.memory_mode,
                        "memory_assisted": i.memory_assisted,
                    }
                    for i in incidents
                ],
                "memory_records": memories,
                "stream_topics": get_bus().topics(),
            }
        finally:
            session.close()

    # ------------------------------------------------------------------ control
    async def start(self, payload: Any) -> dict[str, Any]:
        scenario_id = getattr(payload, "scenario_id", "INC-A1")
        speed = float(getattr(payload, "speed", 4.0) or 4.0)
        auto_analyze = bool(getattr(payload, "auto_analyze", True))
        auto_advance = bool(getattr(payload, "auto_advance", True))
        auto_resolve = bool(getattr(payload, "auto_resolve", False))
        steps = int(getattr(payload, "steps", 0) or 0)

        created = self.orchestrator.create_incident(scenario_id)
        incident_id = created["incident"]["id"]
        run = self._upsert_run(scenario_id=scenario_id, incident_id=incident_id, status="running", speed=speed)
        publish_payload("demo", {"type": "simulation", "status": "started", "scenario_id": scenario_id, "incident_id": incident_id})

        if auto_analyze:
            await self.orchestrator.analyze(incident_id, reason="live simulation started")
        if steps:
            for _ in range(steps):
                await self.orchestrator.advance(incident_id)
        if auto_resolve:
            self.orchestrator.resolve(incident_id, resolved_by="live-simulation")

        if auto_advance:
            self._task = asyncio.create_task(self._run_loop(run["id"], incident_id, auto_analyze))

        return {
            "status": "running",
            "run_id": run["id"],
            "incident_id": incident_id,
            "scenario_id": scenario_id,
            "speed": speed,
            "auto_analyze": auto_analyze,
            "auto_advance": auto_advance,
        }

    def pause(self) -> dict[str, Any]:
        session = self.orchestrator.session()
        try:
            run = session.query(orm.SimulationRun).order_by(orm.SimulationRun.started_at.desc()).first()
            if run is not None:
                run.status = "paused"
                session.commit()
        finally:
            session.close()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        publish_payload("demo", {"type": "simulation", "status": "paused"})
        return {"status": "paused"}

    async def resume(self) -> dict[str, Any]:
        session = self.orchestrator.session()
        try:
            run = session.query(orm.SimulationRun).order_by(orm.SimulationRun.started_at.desc()).first()
            if run is None or not run.incident_id:
                return {"status": "idle", "detail": "nothing to resume"}
            run.status = "running"
            session.commit()
            run_id, incident_id = run.id, run.incident_id
        finally:
            session.close()
        self._task = asyncio.create_task(self._run_loop(run_id, incident_id, True))
        publish_payload("demo", {"type": "simulation", "status": "running"})
        return {"status": "running", "run_id": run_id, "incident_id": incident_id}

    async def _run_loop(self, run_id: str, incident_id: str, auto_analyze: bool) -> None:
        """Advance the scripted simulation on a timer until it is stopped."""
        try:
            while True:
                session = self.orchestrator.session()
                try:
                    run = session.get(orm.SimulationRun, run_id)
                    if run is None or run.status != "running":
                        return
                    delay = 1.0 / max(0.25, run.speed)
                finally:
                    session.close()
                await asyncio.sleep(delay)
                if auto_analyze:
                    await self.orchestrator.advance(incident_id, analyze=True)
                else:
                    await self.orchestrator.advance(incident_id, analyze=False)
        except asyncio.CancelledError:  # pragma: no cover
            return
        except Exception:  # noqa: BLE001 - a background loop must never crash the app
            return

    async def advance_scenario(self, scenario_id: str, *, steps: int = 1, analyze: bool = True) -> dict[str, Any]:
        record = self.orchestrator.load_record(scenario_id)
        if record is None:
            created = self.orchestrator.create_incident(scenario_id)
            incident_id = created["incident"]["id"]
            if analyze:
                await self.orchestrator.analyze(incident_id, reason="scenario advanced from the demo console")
        else:
            incident_id = record.id
        result: dict[str, Any] = {}
        for _ in range(steps):
            result = await self.orchestrator.advance(incident_id, analyze=analyze)
        return {"incident_id": incident_id, **result}

    # ------------------------------------------------------------------ reset
    def reset(self, *, wipe_memory: bool = True) -> dict[str, Any]:
        self.pause()
        reset_db()
        if wipe_memory:
            try:
                self.memory.reset()
            except Exception:  # noqa: BLE001
                pass
        get_bus().clear()
        self.orchestrator.create_incident("INC-A1")
        publish_payload("demo", {"type": "simulation", "status": "reset"})
        return {
            "status": "reset",
            "wiped_memory": wipe_memory,
            "detail": "Database recreated, memory cleared, INC-A1 re-seeded in a known state.",
            "scenarios": self.orchestrator.scenarios.ids(),
        }

    def seed_all(self) -> dict[str, Any]:
        created: list[str] = []
        for scenario in self.orchestrator.scenarios:
            result = self.orchestrator.create_incident(scenario.id)
            created.append(result["incident"]["id"])
        return {"status": "seeded", "incidents": created, "count": len(created)}

    # ------------------------------------------------------------------ scripted demo
    async def run_scripted_demo(self, scenarios: list[str], *, run_comparison: bool = True) -> dict[str, Any]:
        """Run the judge-facing story end to end, deterministically.

        INC-A1 -> diagnostics -> failed restart -> regression -> rollback -> postmortem
        -> memories retained -> INC-A2 (recalls INC-A1) -> resolved -> comparison.
        """
        self.reset(wipe_memory=True)
        story: dict[str, Any] = {"steps": []}

        repeat_scenario = None
        for scenario_id in scenarios:
            if scenario_id.upper().endswith("-A2") or scenario_id.upper() in {"INC-A2", "INC-A1"}:
                if scenario_id.upper() != "INC-A1":
                    repeat_scenario = scenario_id
                    continue
            outcome = await self._drive(scenario_id, memory_enabled=True, follow_plan=True)
            story["steps"].append(outcome)
            if scenario_id.upper() == "INC-A1":
                story["memories_after_a1"] = outcome.get("memory_written", 0)

        # The repeat incident is measured, not staged: the comparison runner drives
        # it twice (memory off / memory on) and reports what actually happened.
        target = repeat_scenario or "INC-A2"
        if run_comparison:
            from recallops.services.comparison import ComparisonService

            comparison = ComparisonService(orchestrator=self.orchestrator, memory=self.memory)
            story["comparison"] = comparison.run(target, persist=True)
            story["steps"].append(
                {
                    "scenario_id": target,
                    "incident_id": story["comparison"]["memory_on"]["incident_id"],
                    "root_cause": story["comparison"]["memory_on"].get("confirmed_cause"),
                    "actions": story["comparison"]["memory_on"]["actions"],
                    "blocked_actions": story["comparison"]["memory_on"]["blocked_actions"],
                    "memory_written": story["comparison"]["memory_on"]["memories_written"],
                    "memory_mode": story["comparison"]["memory_on"]["memory_mode"],
                    "memories_recalled": story["comparison"]["memory_on"]["memories_recalled"],
                }
            )
        else:
            story["steps"].append(await self._drive(target, memory_enabled=True, follow_plan=True))
        story["summary"] = self._summarise(story["steps"])
        return story

    async def _drive(self, scenario_id: str, *, memory_enabled: bool, follow_plan: bool = True) -> dict[str, Any]:
        """Execute the recommended plan until the incident resolves."""
        scenario = self.orchestrator.scenarios.get(scenario_id)
        created = self.orchestrator.create_incident(scenario_id, incident_id=f"{scenario_id}-run")
        incident_id = created["incident"]["id"]
        analysis = await self.orchestrator.analyze(incident_id, memory_enabled=memory_enabled, reason="scripted demo run")
        actions_taken: list[dict[str, Any]] = []
        blocked: list[str] = []
        memory_written = 0

        for _ in range(14):
            record = self.orchestrator.load_record(incident_id)
            if record is None or record.is_resolved:
                break
            pending = [
                a
                for a in record.actions
                if a.result is None and a.status.value in {"awaiting_approval", "proposed", "approved"}
            ]
            if not pending:
                progressed = await self.orchestrator.advance(incident_id)
                if not progressed.get("new_evidence") and progressed.get("stage", {}).get("stage_id") == record.stage_id:
                    break
                continue
            action = pending[0]
            if action.blocked_reason:
                blocked.append(action.id)
                continue
            if action.requires_confirmation:
                self.orchestrator.approve(incident_id, action.id, approved_by="oncall", note="approved by the scripted demo responder")
            result = await self.orchestrator.execute_action(incident_id, action.id, approved_by="oncall")
            actions_taken.append(
                {
                    "action": action.description,
                    "risk": action.risk.value,
                    "executed": result.get("executed", False),
                    "refused": result.get("refused", False),
                    "outcome": (result.get("outcome") or {}).get("outcome"),
                    "helped": (result.get("outcome") or {}).get("helped"),
                }
            )
            if result.get("resolves"):
                break

        resolved = self.orchestrator.resolve(incident_id, resolved_by="incident-commander")
        memory_written = int((resolved.get("memory") or {}).get("written", 0))
        return {
            "scenario_id": scenario_id,
            "incident_id": incident_id,
            "root_cause": (resolved.get("postmortem") or {}).get("root_cause"),
            "actions": actions_taken,
            "blocked_actions": blocked,
            "memory_written": memory_written,
            "memory_mode": analysis["memory"]["mode"],
            "memories_recalled": analysis["memory"]["relevant_count"],
        }

    def _summarise(self, steps: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "incidents": len(steps),
            "root_causes": [s.get("root_cause") for s in steps],
            "total_actions": sum(len(s.get("actions", [])) for s in steps),
            "total_memories_written": sum(int(s.get("memory_written", 0)) for s in steps),
            "memories_recalled": [s.get("memories_recalled") for s in steps],
        }

    # ------------------------------------------------------------------ run rows
    def _upsert_run(self, *, scenario_id: str, incident_id: str, status: str, speed: float) -> dict[str, Any]:
        session = self.orchestrator.session()
        try:
            run = session.query(orm.SimulationRun).order_by(orm.SimulationRun.started_at.desc()).first()
            if run is None:
                run = orm.SimulationRun(id=uuid.uuid4().hex[:12], scenario_id=scenario_id, incident_id=incident_id)
                session.add(run)
            run.scenario_id = scenario_id
            run.incident_id = incident_id
            run.status = status
            run.speed = speed
            run.updated_at = datetime.now(timezone.utc)
            session.commit()
            return {"id": run.id, "scenario_id": run.scenario_id, "status": run.status, "speed": run.speed}
        finally:
            session.close()


__all__ = ["DemoService"]
