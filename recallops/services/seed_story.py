"""Seed the demo with a *played-out* story.

Creating incidents in `NEW` state is not a demo - an empty dashboard with zero
memories looks broken. This module drives the real orchestrator through the
whole learning loop so the app opens on a system that has already:

* investigated a SEV-1 and failed an action,
* resolved it, written a postmortem and retained durable memories,
* hit a repeat incident that recalled those memories and refused the failed action,
* seen a distractor incident where recalled memory had to be contradicted,
* measured memory OFF vs ON.

Everything is produced by the real agent against the real simulator, so the state
on screen is genuine - it is simply further along.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from recallops.agent.orchestrator import IncidentOrchestrator
from recallops.memory.port import MemoryPort

logger = logging.getLogger("recallops.seed")


def _first_action(orchestrator: IncidentOrchestrator, incident_id: str, suffix: str) -> str | None:
    record = orchestrator.load_record(incident_id)
    if record is None:
        return None
    for action in record.actions:
        if action.id.endswith(suffix) and action.result is None:
            return action.id
    return None


def _execute(
    orchestrator: IncidentOrchestrator,
    run: Callable[[Any], Any],
    incident_id: str,
    suffix: str,
    *,
    approve: bool = True,
) -> dict[str, Any] | None:
    action_id = _first_action(orchestrator, incident_id, suffix)
    if not action_id:
        logger.info("seed: %s not offered for %s", suffix, incident_id)
        return None
    if approve:
        try:
            orchestrator.approve(incident_id, action_id, approved_by="oncall", note="approved during demo seeding")
        except Exception as exc:  # noqa: BLE001
            logger.info("seed: approval skipped for %s (%s)", action_id, exc)
    return run(orchestrator.execute_action(incident_id, action_id, approved_by="oncall"))


def _run(coro: Any) -> Any:
    """Execute an orchestrator coroutine from sync code, inside or outside an event loop."""
    import asyncio
    import concurrent.futures

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def seed_story(*, orchestrator: IncidentOrchestrator, memory: MemoryPort, run: Callable[[Any], Any] | None = None) -> dict[str, Any]:
    """Play the whole demo once from sync code, so it works in scripts and on startup."""
    execute = run or _run
    summary: dict[str, Any] = {"incidents": [], "skipped": []}

    # ---------------------------------------------------------------- INC-A1
    try:
        created = orchestrator.create_incident("INC-A1")
        a1 = created["incident"]["id"]
        execute(orchestrator.analyze(a1, reason="demo seed: first analysis (cold start)"))
        summary["incidents"].append({"id": a1, "scenario": "INC-A1", "phase": "analyzed cold start"})

        execute(orchestrator.advance(a1))
        _execute(orchestrator, execute, a1, "inspect_redis_connections")
        failed = _execute(orchestrator, execute, a1, "restart_api_pool")
        if failed:
            summary["incidents"][-1]["failed_action"] = (failed.get("outcome") or {}).get("outcome")
        _execute(orchestrator, execute, a1, "rollback_recent_deploy")
        resolved = orchestrator.resolve(a1, resolved_by="incident-commander")
        summary["incidents"][-1].update(
            {
                "phase": "resolved",
                "root_cause": (resolved.get("postmortem") or {}).get("root_cause"),
                "memories_written": int((resolved.get("memory") or {}).get("written", 0)),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed: INC-A1 failed: %s", exc)
        summary["skipped"].append("INC-A1")

    # ---------------------------------------------------------------- INC-A2
    try:
        orchestrator.create_incident("INC-A2")
        execute(orchestrator.analyze("INC-A2", reason="demo seed: repeat incident (should recall INC-A1)"))
        blocked = [
            a.id
            for a in (orchestrator.load_record("INC-A2").blocked_actions() if orchestrator.load_record("INC-A2") else [])
        ]
        execute(orchestrator.advance("INC-A2"))
        _execute(orchestrator, execute, "INC-A2", "inspect_redis_connections")
        blocked += [
            a.id
            for a in (orchestrator.load_record("INC-A2").blocked_actions() if orchestrator.load_record("INC-A2") else [])
        ]
        _execute(orchestrator, execute, "INC-A2", "rollback_recent_deploy")
        resolved2 = orchestrator.resolve("INC-A2", resolved_by="incident-commander")
        summary["incidents"].append(
            {
                "id": "INC-A2",
                "scenario": "INC-A2",
                "phase": "resolved",
                "blocked_actions": sorted({b.split("~")[-1] for b in blocked}),
                "root_cause": (resolved2.get("postmortem") or {}).get("root_cause"),
                "memories_written": int((resolved2.get("memory") or {}).get("written", 0)),
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed: INC-A2 failed: %s", exc)
        summary["skipped"].append("INC-A2")

    # ---------------------------------------------------------------- INC-B1 (distractor)
    try:
        orchestrator.create_incident("INC-B1")
        execute(orchestrator.advance("INC-B1"))
        record = orchestrator.load_record("INC-B1")
        summary["incidents"].append(
            {
                "id": "INC-B1",
                "scenario": "INC-B1",
                "phase": "investigating",
                "top_hypothesis": record.ranked_hypotheses[0].cause if record and record.ranked_hypotheses else None,
                "conflicts": len(record.conflicts) if record else 0,
            }
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed: INC-B1 failed: %s", exc)
        summary["skipped"].append("INC-B1")

    # ---------------------------------------------- INC-C1 / INC-D1 (triaged only)
    for scenario_id in ("INC-C1", "INC-D1"):
        try:
            orchestrator.create_incident(scenario_id)
            execute(orchestrator.analyze(scenario_id, reason="demo seed: initial triage"))
            summary["incidents"].append({"id": scenario_id, "scenario": scenario_id, "phase": "triaging"})
        except Exception as exc:  # noqa: BLE001
            logger.warning("seed: %s failed: %s", scenario_id, exc)
            summary["skipped"].append(scenario_id)

    # ------------------------------------------------- measured memory OFF vs ON
    try:
        from recallops.services.comparison import ComparisonService

        comparison = ComparisonService(orchestrator=orchestrator, memory=memory)
        result = comparison.run("INC-A2", persist=True)
        summary["comparison"] = {
            "off": {
                "memories": result["memory_off"]["memories_recalled"],
                "actions": result["memory_off"]["actions_executed"],
                "repeated_failed_action": result["memory_off"]["repeated_failed_action"],
            },
            "on": {
                "memories": result["memory_on"]["memories_recalled"],
                "actions": result["memory_on"]["actions_executed"],
                "repeated_failed_action": result["memory_on"]["repeated_failed_action"],
            },
        }
    except Exception as exc:  # noqa: BLE001
        logger.warning("seed: comparison failed: %s", exc)
        summary["comparison"] = None

    summary["memories_total"] = _count(memory)
    logger.info("seed: story ready - %s", summary)
    return summary


def _count(memory: MemoryPort) -> int:
    try:
        listing = memory.list_memories(limit=500)
        return int(listing.get("count", 0) or 0)
    except Exception:  # noqa: BLE001
        return 0


def is_empty(orchestrator: IncidentOrchestrator) -> bool:
    """True when nothing has happened yet (fresh database)."""
    from recallops.persistence import models as orm

    session = orchestrator.session()
    try:
        return session.query(orm.MemoryRecord).count() == 0 and session.query(orm.Postmortem).count() == 0
    finally:
        session.close()


__all__ = ["is_empty", "seed_story"]
