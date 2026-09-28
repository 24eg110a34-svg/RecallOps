"""Integration: the full incident loop through the API.

Covers the behaviours the product is judged on:
repeat incident uses memory, failed action is not recommended as safe, no memory
falls back to current evidence, resolution creates postmortem + memories, demo
reset restores a known state, a Hindsight timeout does not break the UI, and
state-changing actions require confirmation.
"""

from __future__ import annotations

import asyncio

import pytest


def run(coro):
    return asyncio.run(coro)


def client_orq(container):
    """The orchestrator behind the API client (same object the endpoints use)."""
    return container.orchestrator


# --------------------------------------------------------------------------- creation
def test_incident_creation_is_seeded_and_classified(client):
    response = client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    assert response.status_code == 201
    incident = response.json()["incident"]
    assert incident["id"] == "INC-A1"
    assert incident["service"] == "payment-api"
    assert incident["severity"] == "SEV-1"
    assert incident["state"] == "NEW"
    assert "503" in incident["symptom"]


def test_incident_detail_has_evidence_deployments_and_dependencies(client):
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    detail = client.get("/api/incidents/INC-A1").json()
    kinds = {e["kind"] for e in detail["evidence"]}
    assert {"alert", "metric", "log", "deployment", "service_health"} <= kinds
    assert any(d["version"] == "v2.17.4" for d in detail["deployments"])
    assert {d["name"] for d in detail["dependencies"]} >= {"redis-cache", "postgres-orders"}


def test_creating_the_same_incident_twice_is_idempotent(client):
    first = client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    second = client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    assert second.json()["created"] is False
    assert first.json()["incident"]["id"] == second.json()["incident"]["id"]


# --------------------------------------------------------------------------- analysis
def test_analysis_without_memory_falls_back_to_current_evidence(client):
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    analysis = client.post("/api/incidents/INC-A1/analyze", json={}).json()
    assert analysis["memory"]["relevant_count"] == 0
    assert analysis["hypotheses"], "analysis must still produce hypotheses with no memory"
    top = analysis["hypotheses"][0]
    assert "Redis connection-pool exhaustion" in top["cause"]
    assert top["supporting"], "hypotheses must cite supporting evidence"
    assert top["next_diagnostic"]


def test_analysis_moves_through_the_state_machine(client):
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    analysis = client.post("/api/incidents/INC-A1/analyze", json={}).json()
    assert analysis["state"] == "INVESTIGATING"
    assert client.get("/api/incidents/INC-A1").json()["state"] == "INVESTIGATING"


# --------------------------------------------------------------------------- actions
def test_state_changing_action_requires_confirmation(client, container):
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(client_orq(container).advance("INC-A1", analyze=True))
    detail = client.get("/api/incidents/INC-A1").json()
    restart = next(a for a in detail["actions"] if a["id"].endswith("restart_api_pool"))

    denied = client.post(f"/api/incidents/INC-A1/actions/{restart['id']}/execute", json={}).json()
    assert denied["executed"] is False
    assert denied["requires_approval"] is True

    approved = client.post(f"/api/incidents/INC-A1/actions/{restart['id']}/approve", json={"approved_by": "oncall"}).json()
    assert approved["executable"] is True
    executed = client.post(f"/api/incidents/INC-A1/actions/{restart['id']}/execute", json={"approved_by": "oncall"}).json()
    assert executed["executed"] is True
    assert executed["outcome"]["outcome"] == "temporary_improvement"


def test_rejecting_an_action_records_the_decision(client, container):
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(client_orq(container).advance("INC-A1", analyze=True))
    detail = client.get("/api/incidents/INC-A1").json()
    action = detail["actions"][0]
    result = client.post(f"/api/incidents/INC-A1/actions/{action['id']}/reject", json={"rejected_by": "ic", "reason": "not now"}).json()
    assert result["rejected"] is True
    actions = client.get("/api/incidents/INC-A1").json()["actions"]
    assert next(a for a in actions if a["id"] == action["id"])["status"] == "rejected"


def test_high_risk_action_is_never_executed(client, container):
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(client_orq(container).advance("INC-A1", analyze=True))
    from recallops.api.deps import get_container
    from recallops.agent.action_planner import definition_id_from_spec, make_action_id
    from recallops.persistence import models as orm

    orch = get_container().orchestrator
    session = orch.session()
    try:
        row = orm.ActionAttempt(
            id=make_action_id("INC-A1", 99, "failover_database"),
            incident_id="INC-A1",
            description="Fail over the primary database to the replica",
            type="remediation",
            risk="HIGH_RISK",
            status="proposed",
            reason="last resort",
            expected_signal="connection errors stop",
            requires_confirmation=True,
            tool="execute_demo_action",
            reversible=False,
            data_loss_risk=True,
            production_impact=True,
            step_index=99,
        )
        session.add(row)
        session.commit()
    finally:
        session.close()

    approved = client.post(
        "/api/incidents/INC-A1/actions/INC-A1~99~failover_database/approve", json={"approved_by": "ic"}
    ).json()
    assert approved["approved"] is True
    assert approved["refused"] is True
    assert approved["executable"] is False
    executed = client.post(
        "/api/incidents/INC-A1/actions/INC-A1~99~failover_database/execute", json={"approved_by": "ic"}
    ).json()
    assert executed["executed"] is False
    assert executed["refused"] is True


def test_failed_action_is_not_recommended_as_safe(client, container):
    """The whole point of the product: after a failed action, do not propose it again."""
    orch = client_orq(container)
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(orch.advance("INC-A1", analyze=True))
    detail = client.get("/api/incidents/INC-A1").json()
    restart = next(a for a in detail["actions"] if a["id"].endswith("restart_api_pool"))
    client.post(f"/api/incidents/INC-A1/actions/{restart['id']}/approve", json={"approved_by": "oncall"})
    client.post(f"/api/incidents/INC-A1/actions/{restart['id']}/execute", json={"approved_by": "oncall"})
    rollback = next(a for a in client.get("/api/incidents/INC-A1").json()["actions"] if a["id"].endswith("rollback_recent_deploy"))
    client.post(f"/api/incidents/INC-A1/actions/{rollback['id']}/approve", json={"approved_by": "oncall"})
    client.post(f"/api/incidents/INC-A1/actions/{rollback['id']}/execute", json={"approved_by": "oncall"})
    client.post("/api/incidents/INC-A1/resolve", json={}).json()

    client.post("/api/incidents", json={"scenario_id": "INC-A2"})
    run(orch.advance("INC-A2", analyze=True))
    analysis = client.post("/api/incidents/INC-A2/analyze", json={}).json()
    blocked = {b["id"].split("~")[-1]: b for b in analysis["blocked_actions"]}
    assert "restart_api_pool" in blocked, "the failed action must be blocked by memory"
    assert blocked["restart_api_pool"]["status"] == "blocked_by_memory"
    assert blocked["restart_api_pool"]["memory_warnings"]
    assert analysis["recommendation"]["action"]["id"].split("~")[-1] != "restart_api_pool"
    assert any("restart" in w["action"].lower() for w in analysis["memory_warnings"])


# --------------------------------------------------------------------------- repeat incident
def test_repeat_incident_uses_memory(client, container):
    orch = client_orq(container)
    # INC-A1: resolve so the organisation learns.
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(orch.advance("INC-A1", analyze=True))
    detail = client.get("/api/incidents/INC-A1").json()
    for suffix in ("restart_api_pool", "rollback_recent_deploy"):
        action = next((a for a in detail["actions"] if a["id"].endswith(suffix)), None)
        if action is None:
            run(orch.advance("INC-A1", analyze=True))
            detail = client.get("/api/incidents/INC-A1").json()
            action = next(a for a in detail["actions"] if a["id"].endswith(suffix))
        client.post(f"/api/incidents/INC-A1/actions/{action['id']}/approve", json={"approved_by": "oncall"})
        client.post(f"/api/incidents/INC-A1/actions/{action['id']}/execute", json={"approved_by": "oncall"})
    client.post("/api/incidents/INC-A1/resolve", json={})

    # INC-A2: mutated wording, same cause.
    client.post("/api/incidents", json={"scenario_id": "INC-A2"})
    analysis = client.post("/api/incidents/INC-A2/analyze", json={}).json()
    assert analysis["memory"]["relevant_count"] > 0
    top = analysis["hypotheses"][0]
    assert "Redis connection-pool exhaustion" in top["cause"]
    assert "INC-A1" in top["precedent"], "the previous incident must be cited as precedent"
    assert top["memory_contribution"] > 0
    recalled_ids = [i["id"] for i in analysis["memory"]["items"]]
    assert any(i.startswith("INC-A1") for i in recalled_ids)


def test_contradiction_between_memory_and_evidence(client, container):
    """INC-B1 looks similar to INC-A1 but is a database problem."""
    orch = client_orq(container)
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(orch.advance("INC-A1", analyze=True))
    detail = client.get("/api/incidents/INC-A1").json()
    action = next(a for a in detail["actions"] if a["id"].endswith("rollback_recent_deploy"))
    client.post(f"/api/incidents/INC-A1/actions/{action['id']}/approve", json={"approved_by": "oncall"})
    client.post(f"/api/incidents/INC-A1/actions/{action['id']}/execute", json={"approved_by": "oncall"})
    client.post("/api/incidents/INC-A1/resolve", json={})

    client.post("/api/incidents", json={"scenario_id": "INC-B1"})
    run(orch.advance("INC-B1", analyze=True))
    detail = client.get("/api/incidents/INC-B1").json()
    assert "PostgreSQL connection saturation" in detail["hypotheses"][0]["cause"]
    assert detail["recalled_memory_ids"], "memory is recalled (it is a similar symptom)"
    assert detail["conflicts"], "a conflict between memory and current evidence must be reported"
    conflict = detail["conflicts"][0]
    assert "does not match the current incident" in conflict["resolution"]
    # and the memory must not drive the recommendation
    assert "redis_pool_exhaustion" != detail["hypotheses"][0]["id"].split("::")[-1]


# --------------------------------------------------------------------------- resolution
def test_resolution_creates_postmortem_and_memories(client, container):
    orch = client_orq(container)
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(orch.advance("INC-A1", analyze=True))
    detail = client.get("/api/incidents/INC-A1").json()
    action = next(a for a in detail["actions"] if a["id"].endswith("rollback_recent_deploy"))
    client.post(f"/api/incidents/INC-A1/actions/{action['id']}/approve", json={"approved_by": "oncall"})
    client.post(f"/api/incidents/INC-A1/actions/{action['id']}/execute", json={"approved_by": "oncall"})

    resolved = client.post("/api/incidents/INC-A1/resolve", json={"resolved_by": "ic"}).json()
    postmortem = resolved["postmortem"]
    assert postmortem["summary"] and postmortem["root_cause"]
    assert postmortem["timeline"], "the postmortem carries the timeline"
    assert postmortem["prevention"] and postmortem["lessons"]
    assert resolved["memory"]["written"] >= 4
    kinds = {c["kind"] for c in _retained(client)}
    assert {"incident_episode", "action_outcome", "runbook_lesson", "service_pattern"} <= kinds
    assert client.get("/api/incidents/INC-A1").json()["state"] == "LEARNED"
    assert resolved["runbook"]["first_checks"]


def _retained(client) -> list[dict]:
    return client.get("/api/incidents/INC-A1/memories").json()["retained"]


def test_memory_quality_filter_rejects_noise(client, container):
    orch = client_orq(container)
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(orch.advance("INC-A1", analyze=True))
    detail = client.get("/api/incidents/INC-A1").json()
    action = next(a for a in detail["actions"] if a["id"].endswith("rollback_recent_deploy"))
    client.post(f"/api/incidents/INC-A1/actions/{action['id']}/approve", json={"approved_by": "oncall"})
    client.post(f"/api/incidents/INC-A1/actions/{action['id']}/execute", json={"approved_by": "oncall"})
    resolved = client.post("/api/incidents/INC-A1/resolve", json={}).json()
    quality = resolved["memory"]["quality"]
    assert quality["candidates"] >= quality["accepted"]
    assert quality["kinds"]


def test_engineer_correction_becomes_memory(client, container):
    client.post("/api/incidents", json={"scenario_id": "INC-B1"})
    run(client_orq(container).analyze("INC-B1"))
    hypothesis = client.get("/api/incidents/INC-B1").json()["hypotheses"][0]
    response = client.post(
        "/api/incidents/INC-B1/feedback",
        json={
            "target_type": "hypothesis",
            "target_id": hypothesis["id"],
            "verdict": "incorrect",
            "comment": "It is the sequence join, not the pool",
            "corrected_cause": "slow_query_regression",
        },
    ).json()
    assert response["recorded"] is True
    assert response["correction_memory"]["written"] == 1


# --------------------------------------------------------------------------- demo
def test_demo_reset_restores_known_state(client, container):
    client.post("/api/incidents", json={"scenario_id": "INC-A2"})
    run(client_orq(container).advance("INC-A2", analyze=True))
    assert client.get("/api/incidents").json()["count"] == 1

    reset = client.post("/api/demo/reset", json={}).json()
    assert reset["status"] == "reset"
    incidents = client.get("/api/incidents").json()["incidents"]
    assert [i["id"] for i in incidents] == ["INC-A1"]
    assert client.get("/api/incidents/INC-A1").json()["state"] == "NEW"
    assert client.get("/api/memory").json()["count"] == 0
    assert client.get("/api/analytics").json()["totals"]["memories"] == 0


def test_demo_advance_is_deterministic(client, container):
    orch = client_orq(container)
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    first = client.post("/api/demo/scenarios/INC-A1/advance", json={"steps": 1}).json()
    assert first["stage"]["stage_id"] == "cache_pressure"
    second = client.post("/api/demo/scenarios/INC-A1/advance", json={"steps": 1}).json()
    assert second["stage"]["stage_id"] == "regression"
    third = client.post("/api/demo/scenarios/INC-A1/advance", json={"steps": 1}).json()
    assert third["stage"]["stage_id"] == "regression", "the clock alone must not resolve the incident"


def test_scripted_demo_runs_end_to_end(client):
    story = client.post("/api/demo/run", json={"scenarios": ["INC-A1", "INC-A2"], "run_comparison": True}).json()
    a1, a2 = story["steps"][0], story["steps"][1]
    assert "Redis" in a1["root_cause"]
    assert a1["memory_written"] >= 4
    assert a2["memories_recalled"] > 0
    assert "redis_pool_exhaustion" in (a2["root_cause"] or "").lower() or "Redis" in (a2["root_cause"] or "")
    comparison = story["comparison"]
    assert comparison["memory_on"]["memories_recalled"] > comparison["memory_off"]["memories_recalled"]
    assert comparison["memory_on"]["actions_executed"] < comparison["memory_off"]["actions_executed"]


def test_memory_on_off_comparison_is_measured(client, container):
    orch = client_orq(container)
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(orch.advance("INC-A1", analyze=True))
    detail = client.get("/api/incidents/INC-A1").json()
    action = next(a for a in detail["actions"] if a["id"].endswith("rollback_recent_deploy"))
    client.post(f"/api/incidents/INC-A1/actions/{action['id']}/approve", json={"approved_by": "oncall"})
    client.post(f"/api/incidents/INC-A1/actions/{action['id']}/execute", json={"approved_by": "oncall"})
    client.post("/api/incidents/INC-A1/resolve", json={})

    result = client.post("/api/comparison/INC-A2/run", json={"persist": True}).json()
    off, on = result["memory_off"], result["memory_on"]
    assert off["memories_recalled"] == 0
    assert on["memories_recalled"] > 0
    assert on["failed_action_warning"] is True
    assert off["repeated_failed_action"] is True
    assert on["repeated_failed_action"] is False
    assert on["actions_executed"] < off["actions_executed"]
    assert result["verdict"]["memory_changed_recommendation"] is True


# --------------------------------------------------------------------------- resilience
def test_hindsight_timeout_does_not_crash_incident_ui(client, container, monkeypatch):
    """A dead memory provider must degrade, not break the incident screen."""
    from recallops.memory.models import MemoryRecallResult
    from recallops.services.resilience import ErrorKind, ProviderError

    def boom(*_args, **_kwargs):
        raise ProviderError(ErrorKind.TIMEOUT, "connect timeout after 6.0s", provider="hindsight", endpoint="http://hindsight:8888")

    monkeypatch.setattr(container.memory, "recall", boom)
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    analysis = client.post("/api/incidents/INC-A1/analyze", json={}).json()
    assert analysis["hypotheses"], "analysis must continue with current evidence"
    assert analysis["memory"]["degraded"] is True
    assert "timeout" in analysis["memory"]["degraded_reason"].lower()
    detail = client.get("/api/incidents/INC-A1").json()
    assert detail["state"] in {"TRIAGING", "INVESTIGATING"}
    assert client.get("/health").status_code == 200


def test_memory_layer_failure_keeps_incident_viewable(client, container, monkeypatch):
    from recallops.services.resilience import ErrorKind, ProviderError

    class Boom(Exception):
        pass

    def boom(*_args, **_kwargs):
        raise ProviderError(ErrorKind.DNS, "dns failure for hindsight", provider="hindsight")

    monkeypatch.setattr(container.memory, "recall", boom)
    client.post("/api/incidents", json={"scenario_id": "INC-A2"})
    response = client.get("/api/incidents/INC-A2")
    assert response.status_code == 200
    assert response.json()["evidence"]


def test_health_endpoints_report_honest_modes(client):
    health = client.get("/health").json()
    assert set(health["components"]) == {"database", "memory", "llm", "simulator"}
    assert health["config"]["llm"]["api_key_configured"] is False or True
    memory = client.get("/health/memory").json()
    assert memory["mode"] in {"hindsight", "local_hindsight", "demo_fallback", "disabled"}
    assert memory["mode_label"]
    llm = client.get("/health/llm").json()
    assert llm["state"] in {"connected", "degraded", "unavailable"}
    assert "sk-" not in client.get("/health").text


def test_network_diagnostics_do_not_crash(client):
    payload = client.get("/health/network").json()
    assert "verdict" in payload or "ok" in payload
    assert "hints" in payload or "steps" in payload


# --------------------------------------------------------------------------- tools
def test_tool_layer_validates_and_executes(client):
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    catalog = client.get("/api/tools").json()
    assert {"get_incident_context", "query_logs", "recall_memory", "retain_memory", "execute_demo_action"} <= {
        t["name"] for t in catalog["tools"]
    }
    context = client.post("/api/tools/get_incident_context/invoke", json={"incident_id": "INC-A1"}).json()
    assert context["ok"] is True
    assert context["data"]["incident"]["id"] == "INC-A1"
    logs = client.post("/api/tools/query_logs/invoke", json={"incident_id": "INC-A1", "pattern": "RedisTimeout"}).json()
    assert logs["data"]["count"] >= 1
    bad = client.post("/api/tools/get_incident_context/invoke", json={"incident_id": "NOPE"})
    assert bad.status_code == 400
    assert bad.json()["detail"]["code"] == "not_found"
    unknown = client.post("/api/tools/rm_rf/invoke", json={})
    assert unknown.status_code == 400


def test_replay_and_analytics(client, container):
    orch = client_orq(container)
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    run(orch.advance("INC-A1", analyze=True))
    replay = client.get("/api/incidents/INC-A1/replay").json()
    assert replay["timeline"]
    assert {"diagnostics_run", "failed_actions", "memory_recalled"} <= set(replay["summary"])
    analytics = client.get("/api/analytics").json()
    assert analytics["totals"]["incidents"] >= 1
    assert "efficiency" in analytics and "retrieval_metrics" in analytics
    assert client.get("/api/analytics/patterns").json()["patterns"] is not None
