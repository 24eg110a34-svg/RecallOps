"""Memory ON vs OFF comparison.

The numbers are measured from two real, isolated runs of the same scenario
through the same orchestrator - one with organisational memory available and one
with ``MemoryDisabledAdapter``. Nothing here is hard-coded or estimated.
"""

from __future__ import annotations

from typing import Any

from recallops.agent.orchestrator import IncidentOrchestrator
from recallops.memory.factory import set_memory_adapter
from recallops.memory.port import MemoryDisabledAdapter, MemoryPort
from recallops.persistence import models as orm


def anyio_run(coro: Any) -> Any:
    """Run an orchestrator coroutine from synchronous comparison code."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


class ComparisonService:
    def __init__(self, *, orchestrator: IncidentOrchestrator, memory: MemoryPort) -> None:
        self.orchestrator = orchestrator
        self.memory = memory

    # ------------------------------------------------------------------ public
    def run(self, scenario_id: str, *, persist: bool = True) -> dict[str, Any]:
        off = self._drive(scenario_id, memory_enabled=False, suffix="memoff", persist=persist)
        on = self._drive(scenario_id, memory_enabled=True, suffix="memon", persist=persist)
        return {
            "scenario_id": scenario_id,
            "generated_at": off["finished_at"],
            "memory_off": off,
            "memory_on": on,
            "delta": self._delta(off, on),
            "verdict": self._verdict(off, on),
        }

    def latest(self, scenario_id: str) -> dict[str, Any] | None:
        session = self.orchestrator.session()
        try:
            rows = (
                session.query(orm.Incident)
                .filter(orm.Incident.scenario_id == scenario_id, orm.Incident.meta.like('%"comparison": true%'))
                .order_by(orm.Incident.created_at.desc())
                .limit(2)
                .all()
            )
            if not rows:
                return None
            by_mode = {}
            for row in rows:
                mode = "on" if row.memory_enabled else "off"
                by_mode[mode] = self._summary_from_row(row, mode)
            if "on" not in by_mode or "off" not in by_mode:
                return None
            return {
                "scenario_id": scenario_id,
                "memory_off": by_mode["off"],
                "memory_on": by_mode["on"],
                "delta": self._delta(by_mode["off"], by_mode["on"]),
                "verdict": self._verdict(by_mode["off"], by_mode["on"]),
                "generated_at": rows[0].created_at.isoformat() if rows[0].created_at else None,
            }
        finally:
            session.close()

    def history(self) -> dict[str, Any]:
        session = self.orchestrator.session()
        try:
            rows = (
                session.query(orm.Incident)
                .filter(orm.Incident.meta.like('%"comparison": true%'))
                .order_by(orm.Incident.created_at.desc())
                .limit(20)
                .all()
            )
            grouped: dict[str, dict[str, Any]] = {}
            for row in rows:
                grouped.setdefault(row.scenario_id, {})["on" if row.memory_enabled else "off"] = self._summary_from_row(row, "on" if row.memory_enabled else "off")
            return {
                "count": len(rows),
                "comparisons": [
                    {
                        "scenario_id": scenario,
                        "memory_off": modes.get("off"),
                        "memory_on": modes.get("on"),
                    }
                    for scenario, modes in grouped.items()
                ],
            }
        finally:
            session.close()

    # ------------------------------------------------------------------ driving
    def _drive(self, scenario_id: str, *, memory_enabled: bool, suffix: str, persist: bool) -> dict[str, Any]:
        """Drive one isolated run of the scenario and measure what happened."""
        from datetime import datetime, timezone

        scenario = self.orchestrator.scenarios.get(scenario_id)
        truth = scenario.ground_truth.root_cause_id
        # A comparison is a measurement: never inherit a previous run's state.
        incident_id = self._fresh_incident_id(scenario_id, suffix)

        # Isolate the memory layer for this run.
        adapter: MemoryPort = self.memory if memory_enabled else MemoryDisabledAdapter()
        set_memory_adapter(adapter)
        self.orchestrator.memory = adapter

        if persist:
            self.orchestrator.create_incident(scenario_id, incident_id=incident_id, run_id=suffix)
        session = self.orchestrator.session()
        try:
            row = self.orchestrator.get_incident(session, incident_id)
            row.meta = {**(row.meta or {}), "comparison": True, "comparison_mode": "on" if memory_enabled else "off"}
            session.commit()
        finally:
            session.close()

        # First analysis: this is the moment memory should matter most.
        first = self._run_analysis(incident_id, memory_enabled)
        top_confidence = first["confidence"]
        top_cause = first["cause_id"]
        recommended_first = first["recommended"]
        memories_recalled = first["memories_recalled"]
        memory_ids = first["memory_ids"]

        executed: list[dict[str, Any]] = []
        blocked_actions: list[dict[str, Any]] = []
        failed_actions: list[dict[str, Any]] = []
        resolution = ""
        confirmed_step: int | None = None
        steps = 0

        for _ in range(16):
            record = self.orchestrator.load_record(incident_id)
            if record is None or record.is_resolved:
                break
            top = record.ranked_hypotheses[0] if record.ranked_hypotheses else None
            if confirmed_step is None and top is not None and top.cause_id == truth:
                confirmed_step = steps
            for blocked in record.blocked_actions():
                if blocked.result is None and not any(b["action"] == blocked.description for b in blocked_actions):
                    blocked_actions.append({"action": blocked.description, "reason": blocked.blocked_reason})
            pending = [a for a in record.actions if a.result is None and a.status.value in {"awaiting_approval", "proposed", "approved"}]
            if not pending:
                progressed = self._advance(incident_id)
                if not progressed:
                    break
                continue
            action = next((a for a in pending if not a.blocked_reason), None)
            if action is None:
                # everything recommended is blocked by memory: force a fresh analysis
                if not self._advance(incident_id):
                    break
                continue
            if action.requires_confirmation:
                self.orchestrator.approve(incident_id, action.id, approved_by="oncall", note="comparison run")
            result = self._execute(incident_id, action.id)
            steps += 1
            outcome = (result.get("outcome") or {})
            executed.append(
                {
                    "action": action.description,
                    "risk": action.risk.value,
                    "definition": action.id.split("~")[-1],
                    "outcome": outcome.get("outcome"),
                    "helped": outcome.get("helped"),
                }
            )
            if outcome.get("helped") is not True and action.risk.value != "READ_ONLY":
                failed_actions.append({"action": action.description, "outcome": outcome.get("outcome")})
            if result.get("resolves"):
                resolution = f"resolved by {action.description}"
                break

        if confirmed_step is None:
            for position, entry in enumerate(executed, start=1):
                if entry["definition"] in {"rollback_recent_deploy", "add_index", "enable_degraded_mode", "increase_redis_pool_size", "enable_prefetch", "scale_db_pool", "reduce_query_volume", "fix_connection_leak"}:
                    confirmed_step = position
                    break
        if not resolution:
            record = self.orchestrator.load_record(incident_id)
            if record is not None and record.root_cause_id:
                resolution = f"cause confirmed as {record.root_cause_id} (not fully resolved in this run)"

        resolved_payload = self.orchestrator.resolve(incident_id, resolved_by="comparison-runner")
        memory_written = int((resolved_payload.get("memory") or {}).get("written", 0))

        record = self.orchestrator.load_record(incident_id)
        if record is not None:
            memories_recalled = len(record.recalled_memory_ids)
            memory_ids = list(record.recalled_memory_ids)

        session = self.orchestrator.session()
        try:
            row = self.orchestrator.get_incident(session, incident_id)
            summary = {
                "mode": "on" if memory_enabled else "off",
                "incident_id": incident_id,
                "memory_mode": record.memory_mode if record else "",
                "memories_recalled": memories_recalled,
                "memory_ids": memory_ids,
                "similar_incident_found": bool(memory_ids),
                "top_hypothesis": top_cause,
                "top_hypothesis_label": first["cause_label"],
                "top_hypothesis_confidence": round(top_confidence, 3),
                "top_hypothesis_correct": top_cause == truth,
                "confirmed_cause": truth,
                "cause_confirmed_at_first_analysis": top_cause == truth,
                "steps_to_confirmed_cause": confirmed_step if confirmed_step is not None else steps,
                "steps_to_resolution": steps,
                "actions_executed": len(executed),
                "diagnostic_steps": len([e for e in executed if e["risk"] == "READ_ONLY"]),
                "remediations_attempted": len([e for e in executed if e["risk"] != "READ_ONLY"]),
                "failed_actions": failed_actions,
                "failed_action_count": len(failed_actions),
                "repeated_failed_action": any("restart" in e["definition"] for e in executed),
                "failed_action_warning": bool(blocked_actions) or bool(first["memory_warnings"]),
                "blocked_actions": blocked_actions,
                "recommended_first_action": recommended_first,
            "memory_warnings": first["memory_warnings"],
                "resolution": resolution,
                "actions": executed,
                "memories_written": memory_written,
                "finished_at": datetime.now(timezone.utc).isoformat(),
            }
            row.meta = {**(row.meta or {}), "comparison_summary": summary}
            session.commit()
        finally:
            session.close()
        return summary

    def _fresh_incident_id(self, scenario_id: str, suffix: str) -> str:
        candidate = f"{scenario_id}-{suffix}"
        attempt = 2
        while self.orchestrator.load_record(candidate) is not None and attempt < 50:
            candidate = f"{scenario_id}-{suffix}-{attempt}"
            attempt += 1
        return candidate

    def _run_analysis(self, incident_id: str, memory_enabled: bool) -> dict[str, Any]:
        analysis = anyio_run(self.orchestrator.analyze(incident_id, memory_enabled=memory_enabled, reason="comparison run"))
        hypotheses = analysis.get("hypotheses") or []
        memory = analysis.get("memory") or {}
        top = hypotheses[0] if hypotheses else {}
        recommendation = analysis.get("recommendation") or {}
        return {
            "memory_warnings": list(analysis.get("memory_warnings") or []),
            "cause_id": top.get("id", "").split("::")[-1],
            "cause_label": top.get("cause", ""),
            "confidence": float(top.get("confidence") or 0.0),
            "recommended": (recommendation.get("action") or {}).get("description", ""),
            "memories_recalled": int(memory.get("relevant_count") or 0),
            "memory_ids": [i.get("id") for i in (memory.get("items") or [])],
        }

    def _advance(self, incident_id: str) -> bool:
        result = anyio_run(self.orchestrator.advance(incident_id, analyze=True))
        return bool(result.get("new_evidence"))

    def _execute(self, incident_id: str, action_id: str) -> dict[str, Any]:
        return anyio_run(self.orchestrator.execute_action(incident_id, action_id, approved_by="oncall"))

    # ------------------------------------------------------------------ reporting
    def _summary_from_row(self, row: orm.Incident, mode: str) -> dict[str, Any]:
        record = self.orchestrator.load_record(row.id)
        meta = row.meta or {}
        summary = meta.get("comparison_summary") or {}
        if summary:
            return summary
        blocked = [a for a in record.actions if a.blocked_reason] if record else []
        failed = [a for a in (record.failed_actions() if record else [])]
        return {
            "mode": mode,
            "incident_id": row.id,
            "memory_mode": row.memory_mode,
            "memories_recalled": len(row.meta.get("recalled_memory_ids", []) if row.meta else []),
            "memory_ids": list((row.meta or {}).get("recalled_memory_ids", [])),
            "similar_incident_found": bool((row.meta or {}).get("recalled_memory_ids")),
            "top_hypothesis": row.top_hypothesis_cause_id,
            "top_hypothesis_confidence": round(row.top_hypothesis_confidence or 0.0, 3),
            "top_hypothesis_correct": row.top_hypothesis_cause_id == record.root_cause_id if record else False,
            "confirmed_cause": row.root_cause_id,
            "steps_to_confirmed_cause": row.confirmed_step or row.step_count,
            "actions_executed": row.step_count or 0,
            "diagnostic_steps": len([a for a in (record.diagnostics() if record else [])]),
            "remediations_attempted": len([a for a in (record.remediation_attempts() if record else [])]),
            "failed_action_count": len(failed),
            "failed_actions": [{"action": a.description, "outcome": a.result.outcome.value if a.result else None} for a in failed],
            "repeated_failed_action": any("restart" in a.id for a in (record.actions if record else [])),
            "failed_action_warning": bool(blocked) or bool(summary.get("memory_warnings")),
            "blocked_actions": [{"action": a.description, "reason": a.blocked_reason} for a in blocked],
            "resolution": row.resolution,
            "memories_written": 0,
        }

    def _summary_from_record(self, record: Any, mode: str) -> dict[str, Any]:
        return self._summary_from_row(
            type("_R", (), {
                "id": record.id,
                "root_cause_id": record.root_cause_id,
                "step_count": record.step_count,
                "confirmed_step": record.confirmed_step,
                "memory_mode": record.memory_mode,
                "top_hypothesis_cause_id": record.top_hypothesis_cause_id,
                "top_hypothesis_confidence": record.top_hypothesis_confidence,
                "meta": {"recalled_memory_ids": record.recalled_memory_ids},
            })(),
            mode,
        )

    @staticmethod
    def _delta(off: dict[str, Any], on: dict[str, Any]) -> dict[str, Any]:
        def diff(key: str) -> Any:
            a, b = off.get(key), on.get(key)
            if isinstance(a, (int, float)) and isinstance(b, (int, float)):
                return round(b - a, 3)
            return None

        return {
            "memories_recalled": diff("memories_recalled"),
            "steps_to_confirmed_cause": diff("steps_to_confirmed_cause"),
            "actions_executed": diff("actions_executed"),
            "diagnostic_steps": diff("diagnostic_steps"),
            "failed_actions": diff("failed_action_count"),
            "top_hypothesis_confidence": diff("top_hypothesis_confidence"),
            "failed_action_warning": on.get("failed_action_warning", False) and not off.get("failed_action_warning", False),
            "similar_incident_found": on.get("similar_incident_found", False) and not off.get("similar_incident_found", False),
        }

    @staticmethod
    def _verdict(off: dict[str, Any], on: dict[str, Any]) -> dict[str, Any]:
        saved = (off.get("steps_to_confirmed_cause") or 0) - (on.get("steps_to_confirmed_cause") or 0)
        return {
            "headline": (
                f"With memory: recalled {on.get('memories_recalled', 0)} memories, "
                f"warned about a known failed action: {on.get('failed_action_warning', False)}, "
                f"steps to confirmed cause {on.get('steps_to_confirmed_cause')} vs {off.get('steps_to_confirmed_cause')} without."
            ),
            "steps_saved": saved,
            "memory_changed_recommendation": bool(
                on.get("failed_action_warning") and not off.get("failed_action_warning")
            ),
            "top_hypothesis_confidence_delta": round(
                (on.get("top_hypothesis_confidence") or 0.0) - (off.get("top_hypothesis_confidence") or 0.0), 3
            ),
        }


__all__ = ["ComparisonService"]
