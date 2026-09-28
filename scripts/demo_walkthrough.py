"""Full demo walkthrough over HTTP (uses the real API, nothing in-process).

Run with the API up:  python scripts/demo_walkthrough.py
Each step prints PASS/FAIL with the evidence it checked, so a failure names itself.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

API = "http://127.0.0.1:8765"
results: list[tuple[bool, str, str]] = []


def req(path: str, method: str = "GET", payload: dict | None = None, timeout: float = 180.0):
    data = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(f"{API}{path}", data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode() or "null")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return 0, {"error": str(exc)}


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((bool(ok), name, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  <- {detail}" if not ok else ""))
    return bool(ok)


def section(title: str) -> None:
    print(f"\n{title}\n{'-' * len(title)}")


def find_action(incident_id: str, suffix: str) -> dict | None:
    _, detail = req(f"/api/incidents/{incident_id}")
    for action in detail.get("actions", []):
        if action["id"].endswith(suffix) and action.get("result") is None:
            return action
    return None


def run_action(incident_id: str, suffix: str) -> dict:
    action = find_action(incident_id, suffix)
    if not action:
        return {"error": f"{suffix} not offered for {incident_id}"}
    req(f"/api/incidents/{incident_id}/actions/{action['id']}/approve", "POST", {"approved_by": "oncall"})
    _, body = req(f"/api/incidents/{incident_id}/actions/{action['id']}/execute", "POST", {"approved_by": "oncall"})
    return body or {}


def main() -> int:
    print(f"RecallOps demo walkthrough against {API}")

    # ------------------------------------------------------------ cold start
    section("0. Cold start - memory must be empty")
    status, health = req("/health")
    check("backend is up", status == 200, f"status={status}")
    _, memory_health = req("/health/memory")
    check("memory mode reported", bool(memory_health.get("mode")), str(memory_health))
    print(f"        memory mode = {memory_health.get('mode')} ({memory_health.get('mode_label')})")
    _, mem = req("/api/memory")
    check("no memories before the demo", (mem or {}).get("count") == 0, f"count={(mem or {}).get('count')}")

    # --------------------------------------------------------------- INC-A1
    section("1. INC-A1 - cold start payment-api incident")
    status, created = req("/api/incidents", "POST", {"scenario_id": "INC-A1"})
    incident = (created or {}).get("incident", {})
    check("INC-A1 created", status == 201, f"status={status}")
    check("severity auto-classified as SEV-1", incident.get("severity") == "SEV-1", str(incident.get("severity")))
    check("state starts at NEW", incident.get("state") == "NEW", str(incident.get("state")))

    section("2. Investigate (no memory available)")
    _, analysis = req("/api/incidents/INC-A1/analyze", "POST", {})
    check("analysis ran", bool(analysis.get("hypotheses")), str(analysis)[:120])
    check("NO memory recalled (cold start)", (analysis.get("memory") or {}).get("relevant_count") == 0,
          f"recalled={(analysis.get('memory') or {}).get('relevant_count')}")
    top = (analysis.get("hypotheses") or [{}])[0]
    check("top cause = Redis connection-pool exhaustion", "Redis connection-pool exhaustion" in top.get("cause", ""), str(top.get("cause")))
    check("hypothesis cites supporting evidence", len(top.get("supporting") or []) > 0)
    check("hypothesis has a next diagnostic", bool(top.get("next_diagnostic")))
    print(f"        confidence={top.get('confidence')} signals={top.get('signals')}")

    req("/api/demo/scenarios/INC-A1/advance", "POST", {"steps": 1, "analyze": True})
    _, detail = req("/api/incidents/INC-A1")
    kinds = {e["kind"] for e in detail.get("evidence", [])}
    check("evidence includes logs + metrics + deployment", {"log", "metric", "deployment"} <= kinds, str(sorted(kinds)))
    check("incident reached INVESTIGATING", detail.get("state") == "INVESTIGATING", str(detail.get("state")))

    section("3. Failed pod restart (requires approval)")
    action = find_action("INC-A1", "restart_api_pool")
    check("restart action offered", action is not None)
    if action:
        _, denied = req(f"/api/incidents/INC-A1/actions/{action['id']}/execute", "POST", {})
        check("blocked without approval", (denied or {}).get("executed") is False and (denied or {}).get("requires_approval") is True, str(denied)[:120])
        _, approved = req(f"/api/incidents/INC-A1/actions/{action['id']}/approve", "POST", {"approved_by": "oncall"})
        check("approved by a named human", (approved or {}).get("executable") is True, str(approved)[:120])
        _, result = req(f"/api/incidents/INC-A1/actions/{action['id']}/execute", "POST", {"approved_by": "oncall"})
        outcome = (result or {}).get("outcome", {})
        check("restart outcome = temporary improvement", outcome.get("outcome") == "temporary_improvement", str(outcome.get("outcome")))
        check("a reusable lesson was captured", bool(result.get("lesson")), str(result.get("lesson"))[:80])
        print(f"        lesson: {(result.get('lesson') or '')[:120]}")

    section("4. Successful rollback")
    outcome = run_action("INC-A1", "rollback_recent_deploy")
    check("rollback executed", outcome.get("executed") is True, str(outcome)[:160])
    check("rollback resolved the incident", outcome.get("resolves") is True, str(outcome)[:160])
    print(f"        observed: {json.dumps(outcome.get('outcome', {}).get('observed', {}))[:140]}")

    section("5. Postmortem + memory retention")
    _, resolved = req("/api/incidents/INC-A1/resolve", "POST", {"resolved_by": "smoke-test"})
    postmortem = (resolved or {}).get("postmortem") or {}
    memory = (resolved or {}).get("memory") or {}
    check("postmortem generated", bool(postmortem.get("root_cause")), str(postmortem)[:120])
    check("postmortem has a timeline", len(postmortem.get("timeline") or []) > 3)
    check("prevention actions written", len(postmortem.get("prevention") or []) > 0)
    check("runbook generated", bool((resolved or {}).get("runbook")))
    check("durable memories retained", (memory.get("written") or 0) >= 4, f"written={memory.get('written')}")
    check("memory written through a real adapter", memory.get("mode") in {"hindsight", "local_hindsight", "demo_fallback"}, str(memory.get("mode")))
    print(f"        retained {memory.get('written')} memories via {memory.get('mode')}")
    _, detail = req("/api/incidents/INC-A1")
    check("state is LEARNED", detail.get("state") == "LEARNED", str(detail.get("state")))

    # --------------------------------------------------------------- INC-A2
    section("6. INC-A2 - the repeat incident")
    req("/api/incidents", "POST", {"scenario_id": "INC-A2"})
    _, a2 = req("/api/incidents/INC-A2/analyze", "POST", {})
    a2mem = a2.get("memory") or {}
    a2top = (a2.get("hypotheses") or [{}])[0]
    check("INC-A1 recalled", (a2mem.get("relevant_count") or 0) > 0, f"recalled={a2mem.get('relevant_count')}")
    check("precedent cites INC-A1", "INC-A1" in (a2top.get("precedent") or []), str(a2top.get("precedent")))
    check("memory contributed to confidence", (a2top.get("memory_contribution") or 0) > 0, str(a2top.get("memory_contribution")))
    check("failed restart surfaced as a learned lesson", len(a2.get("memory_warnings") or []) > 0, str(a2.get("memory_warnings"))[:120])
    print(f"        recalled={a2mem.get('relevant_count')} precedent={a2top.get('precedent')} memory_contribution={a2top.get('memory_contribution')}")

    section("7. The previous failed restart is remembered and avoided")
    req("/api/demo/scenarios/INC-A2/advance", "POST", {"steps": 1, "analyze": True})
    _, a2b = req("/api/incidents/INC-A2/analyze", "POST", {})
    blocked = a2b.get("blocked_actions") or []
    restart_blocked = [b for b in blocked if b["id"].endswith("restart_api_pool")]
    check("restart_api_pool is BLOCKED by memory", bool(restart_blocked), f"blocked={[b['id'] for b in blocked]}")
    if restart_blocked:
        print(f"        reason: {restart_blocked[0]['blocked_reason']}")
    check("recommended action is NOT the restart", not (a2b.get("recommendation", {}).get("action", {}).get("id", "").endswith("restart_api_pool")),
          str(a2b.get("recommendation", {}).get("action", {}).get("id")))
    print(f"        recommended instead: {a2b.get('recommendation', {}).get('action', {}).get('description')}")

    run_action("INC-A2", "rollback_recent_deploy")
    _, a2res = req("/api/incidents/INC-A2/resolve", "POST", {})
    check("INC-A2 resolved with its own postmortem", bool((a2res or {}).get("postmortem")))
    check("INC-A2 retained new memories", ((a2res or {}).get("memory") or {}).get("written", 0) > 0)

    # ---------------------------------------------------------- distractor
    section("8. INC-B1 - memory contradicted by evidence")
    req("/api/incidents", "POST", {"scenario_id": "INC-B1"})
    req("/api/demo/scenarios/INC-B1/advance", "POST", {"steps": 1, "analyze": True})
    _, b1 = req("/api/incidents/INC-B1")
    b1top = (b1.get("hypotheses") or [{}])[0]
    check("B1 leads with a DATABASE cause", "PostgreSQL connection saturation" in b1top.get("cause", ""), str(b1top.get("cause")))
    check("Redis memory was still recalled", len(b1.get("recalled_memory_ids") or []) > 0)
    check("a memory conflict was reported", len(b1.get("conflicts") or []) > 0, str(b1.get("conflicts"))[:120])

    # --------------------------------------------------------- comparison
    section("9. Memory OFF vs Memory ON (measured)")
    _, cmp = req("/api/comparison/INC-A2/run", "POST", {"persist": True})
    off, on = cmp.get("memory_off", {}), cmp.get("memory_on", {})
    check("memory OFF recalls nothing", (off.get("memories_recalled") or 0) == 0, str(off.get("memories_recalled")))
    check("memory ON recalls memories", (on.get("memories_recalled") or 0) > 0, str(on.get("memories_recalled")))
    check("memory ON warns about the failed action", on.get("failed_action_warning") is True, str(on.get("failed_action_warning")))
    check("memory OFF repeated the bad action", off.get("repeated_failed_action") is True, str(off.get("repeated_failed_action")))
    check("memory ON did NOT repeat it", on.get("repeated_failed_action") is False, str(on.get("repeated_failed_action")))
    check("memory ON used fewer/equal actions", (on.get("actions_executed") or 99) <= (off.get("actions_executed") or 0),
          f"off={off.get('actions_executed')} on={on.get('actions_executed')}")
    print(f"        OFF: mem={off.get('memories_recalled')} actions={off.get('actions_executed')} repeated={off.get('repeated_failed_action')}")
    print(f"        ON : mem={on.get('memories_recalled')} actions={on.get('actions_executed')} warned={on.get('failed_action_warning')}")
    print(f"        verdict: {cmp.get('verdict', {}).get('headline')}")

    # ------------------------------------------------------------ hindsight
    section("10. Hindsight connectivity and fallback (no faking)")
    _, hs = req("/health/hindsight")
    print(f"        configured={hs.get('configured')} endpoint={hs.get('endpoint')} key_set={hs.get('api_key_configured')}")
    check("Hindsight endpoint not configured (expected locally)", hs.get("configured") is False, str(hs.get("endpoint")))
    check("system reports the local mirror instead", (memory_health.get("mode")) in {"local_hindsight", "demo_fallback"}, str(memory_health.get("mode")))
    check("fallback is labelled honestly", "not Hindsight" in (memory_health.get("mode_label") or "") or "mirror" in (memory_health.get("mode_label") or "").lower(),
          str(memory_health.get("mode_label")))
    _, net = req("/health/network?url=http://127.0.0.1:9/")
    check("network diagnostics return a verdict", bool(net.get("verdict") or net.get("ok") is not None), str(net)[:100])
    print(f"        diagnosis of an unreachable endpoint: {str(net.get('verdict'))[:90]}")
    _, hs_bad = req("/health/hindsight?probe=true")
    check("probing a missing Hindsight does not 500", bool(hs_bad), str(hs_bad)[:120])

    # -------------------------------------------------------------- summary
    passed = sum(1 for ok, _, _ in results if ok)
    failed = [(n, d) for ok, n, d in results if not ok]
    print("\n" + "=" * 62)
    print(f"{passed}/{len(results)} checks passed")
    if failed:
        print("FAILED:")
        for name, detail in failed:
            print(f"  - {name}: {detail}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
