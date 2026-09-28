"""Smoke test: is the whole site actually working?

    python scripts/smoke_test.py

Checks the backend (":8765"), the frontend (":4321") and the data behind every
page. Prints PASS/FAIL per check and exits non-zero if anything is broken.
Add --verbose to also print what the API returned.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

API = "http://127.0.0.1:8765"
WEB = "http://127.0.0.1:4321"

PAGES = [
    "/",
    "/incidents",
    "/incidents/INC-A1",
    "/memory",
    "/graph",
    "/replay",
    "/analytics",
    "/runbooks",
    "/postmortems",
    "/settings/health",
]

results: list[tuple[bool, str, str]] = []


def check(name: str, condition: bool, detail: str = "") -> bool:
    results.append((bool(condition), name, detail))
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {name}" + (f"  ({detail})" if detail and not condition else ""))
    return bool(condition)


def get(url: str, timeout: float = 30.0) -> tuple[int, str]:
    try:
        with urlopen(Request(url, headers={"Accept": "*/*"}), timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (URLError, TimeoutError, OSError) as exc:
        return 0, str(exc)


def get_json(url: str, timeout: float = 60.0) -> Any:
    status, body = get(url, timeout)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


def section(title: str) -> None:
    print(f"\n{title}")
    print("-" * len(title))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    print("RecallOps smoke test")
    print(f"  api: {API}")
    print(f"  web: {WEB}")

    # ---------------------------------------------------------------- backend
    section("1. Backend")
    health = get_json(f"{API}/health")
    if not check("backend is up", health is not None, "cannot reach " + API):
        print("\nStart it with:")
        print("  .\\.venv\\Scripts\\python -m uvicorn recallops.main:app --host 127.0.0.1 --port 8765")
        return 1

    check("health reports ok", health.get("status") in {"ok", "degraded"}, str(health.get("status")))
    components = health.get("components", {})
    for name in ("database", "memory", "llm", "simulator"):
        info = components.get(name, {})
        check(f"component: {name}", bool(info), "missing")
    if args.verbose:
        print(json.dumps(components, indent=2))

    memory = get_json(f"{API}/health/memory") or {}
    check("memory layer reports a mode", bool(memory.get("mode")), str(memory))
    check("memory layer is honest about its mode", bool(memory.get("mode_label")), "no mode_label")

    for endpoint in ("/api/demo/scenarios", "/api/tools", "/api/actions/catalog"):
        check(f"endpoint {endpoint}", get_json(f"{API}{endpoint}") is not None)

    # ------------------------------------------------------------------- data
    section("2. Demo data (the app must not be empty)")
    incidents = get_json(f"{API}/api/incidents") or {}
    check("incidents exist", (incidents.get("count") or 0) > 0, "0 incidents - press 'Reset demo' or wait for auto-seed")

    analytics = get_json(f"{API}/api/analytics") or {}
    totals = analytics.get("totals", {})
    check("at least one incident resolved", (totals.get("resolved") or 0) > 0, f"resolved={totals.get('resolved')}")
    check("memories were created", (totals.get("memories") or 0) > 0, f"memories={totals.get('memories')}")
    check("postmortem was generated", (totals.get("postmortems") or 0) > 0, f"postmortems={totals.get('postmortems')}")
    check("runbook was generated", (totals.get("runbooks") or 0) > 0, f"runbooks={totals.get('runbooks')}")
    check("analytics has a top root cause", bool(analytics.get("top_root_causes")), "no root causes")
    if args.verbose:
        print(json.dumps(totals, indent=2))

    # ----------------------------------------------------------- learning loop
    section("3. The learning loop")
    detail = get_json(f"{API}/api/incidents/INC-A1") or {}
    check("INC-A1 has a confirmed root cause", bool(detail.get("root_cause")), str(detail.get("state")))
    check("INC-A1 timeline is recorded", (detail.get("events") or detail.get("timeline") or []).__len__() > 3)
    phases = {e.get("phase") for e in (detail.get("events") or [])}
    for phase in ("incident", "evidence", "memory_recall", "hypothesis", "recommendation", "resolution", "memory"):
        check(f"timeline has '{phase}' events", phase in phases, str(sorted(phases)))

    memories = get_json(f"{API}/api/memory") or {}
    kinds = {m.get("kind") for m in memories.get("items", [])}
    check("incident episodes retained", "incident_episode" in kinds, str(sorted(kinds)))
    check("action outcomes retained", "action_outcome" in kinds, str(sorted(kinds)))
    check("service patterns retained", "service_pattern" in kinds, str(sorted(kinds)))

    a2 = get_json(f"{API}/api/incidents/INC-A2") or {}
    check("INC-A2 (repeat) cited memory", len(a2.get("recalled_memory_ids") or []) > 0, "no memories cited")
    blocked = [a.get("id", "") for a in (a2.get("actions") or []) if a.get("status") == "blocked_by_memory"]
    check("the known-bad action was blocked", any("restart" in b for b in blocked), f"blocked={blocked}")

    comparison = get_json(f"{API}/api/comparison/INC-A2")
    if comparison:
        off, on = comparison.get("memory_off", {}), comparison.get("memory_on", {})
        check("comparison: memory ON recalls more", (on.get("memories_recalled") or 0) > (off.get("memories_recalled") or 0),
              f"off={off.get('memories_recalled')} on={on.get('memories_recalled')}")
        check("comparison: memory ON avoids the failed action",
              bool(on.get("failed_action_warning")) and not bool(off.get("failed_failed_action_warning") or off.get("failed_action_warning")),
              f"off={off.get('failed_action_warning')} on={on.get('failed_action_warning')}")
        check("comparison: memory ON repeats less", (on.get("actions_executed") or 99) <= (off.get("actions_executed") or 0),
              f"off={off.get('actions_executed')} on={on.get('actions_executed')}")
    else:
        check("comparison run exists", False, "POST /api/comparison/INC-A2/run first")

    graph = get_json(f"{API}/api/memory/graph") or {}
    check("memory graph has nodes", len(graph.get("nodes") or []) > 0)
    check("memory graph has edges", len(graph.get("edges") or []) > 0)

    replay = get_json(f"{API}/api/incidents/INC-A1/replay") or {}
    check("replay is available", bool(replay.get("timeline")))

    # --------------------------------------------------------------- frontend
    section("4. Frontend pages")
    status, body = get(f"{WEB}/", timeout=60)
    if not check("frontend is up", status == 200, f"{status} - start it with: cd apps/web && npm run dev"):
        print("\nStart it with:")
        print("  cd apps/web")
        print("  npm run dev")
    else:
        check("frontend renders the shell", "RECALL" in body.upper(), "brand not found in HTML")
        for page in PAGES:
            status, page_body = get(f"{WEB}{page}", timeout=60)
            check(f"page {page}", status == 200 and len(page_body) > 200, f"status={status} len={len(page_body)}")

    # ------------------------------------------------------------------ report
    passed = sum(1 for ok, _, _ in results if ok)
    failed = [(name, detail) for ok, name, detail in results if not ok]
    print("\n" + "=" * 60)
    print(f"{passed}/{len(results)} checks passed")
    if failed:
        print("\nFailed:")
        for name, detail in failed:
            print(f"  - {name} {detail}")
        return 1
    print("Everything is working. Open " + WEB)
    return 0


if __name__ == "__main__":
    sys.exit(main())
