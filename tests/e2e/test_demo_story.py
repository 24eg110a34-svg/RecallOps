"""End-to-end smoke tests: the full judge-facing demo story, in order.

These exercise the same sequence a presenter clicks through:
INC-A1 -> diagnostics -> failed restart -> regression -> rollback -> postmortem
-> memories retained -> INC-A2 recalls INC-A1 -> avoids the failed action ->
comparison -> analytics.
"""

from __future__ import annotations

import pytest


def _action(client, incident_id: str, suffix: str) -> dict:
    detail = client.get(f"/api/incidents/{incident_id}").json()
    matches = [a for a in detail["actions"] if a["id"].endswith(suffix)]
    assert matches, f"action {suffix} not offered for {incident_id}: {[a['id'] for a in detail['actions']]}"
    return matches[0]


def _run(client, incident_id: str, suffix: str, approve: bool = True) -> dict:
    action = _action(client, incident_id, suffix)
    if approve:
        client.post(f"/api/incidents/{incident_id}/actions/{action['id']}/approve", json={"approved_by": "oncall"})
    return client.post(
        f"/api/incidents/{incident_id}/actions/{action['id']}/execute", json={"approved_by": "oncall"}
    ).json()


@pytest.mark.e2e
def test_full_demo_story(client):
    # 1. reset to a known state
    assert client.post("/api/demo/reset", json={}).status_code == 200

    # 2. INC-A1 opens, cold start: no organisational memory
    created = client.post("/api/incidents", json={"scenario_id": "INC-A1"}).json()
    assert created["incident"]["severity"] == "SEV-1"
    analysis = client.post("/api/incidents/INC-A1/analyze", json={}).json()
    assert analysis["memory"]["relevant_count"] == 0
    assert "Redis connection-pool exhaustion" in analysis["hypotheses"][0]["cause"]

    # 3. more evidence arrives
    client.post("/api/demo/scenarios/INC-A1/advance", json={"steps": 1, "analyze": True})
    _run(client, "INC-A1", "inspect_redis_connections")

    # 4. the restart is tried - and remembered as a failure
    restart = _run(client, "INC-A1", "restart_api_pool")
    assert restart["outcome"]["outcome"] == "temporary_improvement"
    assert "restart" in restart["lesson"].lower()

    # 5. rollback resolves the incident
    rollback = _run(client, "INC-A1", "rollback_recent_deploy")
    assert rollback["resolves"] is True

    # 6. postmortem + durable memory
    resolved = client.post("/api/incidents/INC-A1/resolve", json={}).json()
    assert resolved["postmortem"]["root_cause"]
    assert resolved["memory"]["written"] >= 4
    assert client.get("/api/incidents/INC-A1").json()["state"] == "LEARNED"

    # 7. INC-A2: the repeat incident benefits from that learning
    client.post("/api/incidents", json={"scenario_id": "INC-A2"})
    repeat = client.post("/api/incidents/INC-A2/analyze", json={}).json()
    assert repeat["memory"]["relevant_count"] > 0
    assert "INC-A1" in repeat["hypotheses"][0]["precedent"]
    assert repeat["memory_warnings"], "the failed action must be surfaced as a learned lesson"

    client.post("/api/demo/scenarios/INC-A2/advance", json={"steps": 1, "analyze": True})
    blocked = client.post("/api/incidents/INC-A2/analyze", json={}).json()["blocked_actions"]
    assert any(b["id"].endswith("restart_api_pool") for b in blocked), "the known failed action must be blocked"

    # 8. resolution retains the new learning
    _run(client, "INC-A2", "rollback_recent_deploy")
    a2 = client.post("/api/incidents/INC-A2/resolve", json={}).json()
    assert a2["memory"]["written"] >= 3

    # 9. the timeline told the whole story
    replay = client.get("/api/incidents/INC-A1/replay").json()
    phases = [e["phase"] for e in replay["timeline"]]
    for phase in ("incident", "evidence", "memory_recall", "hypothesis", "recommendation", "approval", "action", "outcome", "resolution", "memory"):
        assert phase in phases, f"timeline is missing the {phase} phase"
    assert replay["summary"]["failed_actions"] >= 1

    # 10. analytics, graph and runbooks reflect the learning
    analytics = client.get("/api/analytics").json()
    assert analytics["totals"]["incidents"] >= 2
    assert analytics["totals"]["memories"] >= 8
    assert analytics["top_root_causes"][0]["cause_id"] == "redis_pool_exhaustion"
    graph = client.get("/api/memory/graph").json()
    assert graph["nodes"] and graph["edges"]
    assert client.get("/api/runbooks").json()["count"] >= 1
    assert client.get("/api/postmortems").json()["count"] >= 2

    # 11. memory ON vs OFF, measured
    comparison = client.post("/api/comparison/INC-A2/run", json={"persist": True}).json()
    off, on = comparison["memory_off"], comparison["memory_on"]
    assert off["memories_recalled"] == 0
    assert on["memories_recalled"] > 0
    assert on["failed_action_warning"] is True
    assert on["repeated_failed_action"] is False
    # With a clean slate (see tests/integration) memory ON also needs strictly
    # fewer actions; here we only require it is no worse.
    assert on["actions_executed"] <= off["actions_executed"]


@pytest.mark.e2e
def test_scripted_demo_endpoint_runs_the_story(client):
    story = client.post("/api/demo/run", json={"scenarios": ["INC-A1", "INC-A2"], "run_comparison": True}).json()
    assert story["summary"]["incidents"] >= 2
    assert story["summary"]["total_memories_written"] >= 6
    assert story["comparison"]["memory_on"]["memories_recalled"] > story["comparison"]["memory_off"]["memories_recalled"]
    assert story["comparison"]["verdict"]["memory_changed_recommendation"] is True


@pytest.mark.e2e
def test_incident_timeline_endpoint_replays_the_story(client):
    """The SSE transport is covered in tests/unit; here we assert the persisted timeline."""
    client.post("/api/incidents", json={"scenario_id": "INC-A1"})
    client.post("/api/incidents/INC-A1/analyze", json={})
    history = client.get("/api/incidents/INC-A1/events").json()
    assert history["count"] > 3
    assert history["events"][0]["phase"] == "incident"
    phases = [e["phase"] for e in history["events"]]
    assert "memory_recall" in phases and "hypothesis" in phases and "recommendation" in phases
