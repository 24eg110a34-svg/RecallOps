"""Run the evaluation: memory OFF vs memory ON, plus a scoring table.

    python scripts/run_eval.py                       # eval INC-A2
    python scripts/run_eval.py --scenario INC-B1
    python scripts/run_eval.py --json out.json

Everything printed here is measured from real orchestrator runs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recallops.api.deps import build_container, get_container, set_container  # noqa: E402
from recallops.memory.port import MemoryDisabledAdapter  # noqa: E402
from recallops.persistence.db import reset_db  # noqa: E402
from recallops.services.comparison import ComparisonService  # noqa: E402
from recallops.services.demo import DemoService  # noqa: E402


def _row(label: str, off: object, on: object, better: str = "up") -> str:
    def fmt(value: object) -> str:
        if isinstance(value, bool):
            return "yes" if value else "no"
        if isinstance(value, float):
            return f"{value:.2f}"
        return str(value)

    arrow = ""
    if isinstance(off, (int, float)) and isinstance(on, (int, float)):
        delta = float(on) - float(off)
        good = delta > 0 if better == "up" else delta < 0
        arrow = "  <-- better" if good else ("  <-- worse" if delta else "")
    return f"{label:34s} {fmt(off):>12s} {fmt(on):>12s}{arrow}"


async def main_async(scenario: str, reset: bool) -> dict:
    if reset:
        reset_db()
        set_container(build_container())
    container = get_container()
    orchestrator = container.orchestrator

    # Seed the learning first: resolve the seed incident so memory exists.
    demo = DemoService(orchestrator=orchestrator, memory=container.memory, session_factory=orchestrator.session)
    seed = await demo._drive("INC-A1", memory_enabled=True)
    print(f"seeded learning from INC-A1: {seed['memory_written']} memories retained\n")

    comparison = ComparisonService(orchestrator=orchestrator, memory=container.memory)
    result = comparison.run(scenario, persist=True)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="RecallOps evaluation (memory off vs on)")
    parser.add_argument("--scenario", default="INC-A2")
    parser.add_argument("--keep", action="store_true", help="do not reset the database first")
    parser.add_argument("--json", default="", help="write the raw result to this path")
    args = parser.parse_args()

    result = asyncio.run(main_async(args.scenario, reset=not args.keep))
    off, on = result["memory_off"], result["memory_on"]

    print(f"\nMemory OFF vs Memory ON - {result['scenario_id']}")
    print("=" * 72)
    print(f"{'metric':34s} {'memory OFF':>12s} {'memory ON':>12s}")
    print("-" * 72)
    print(_row("memories recalled", off["memories_recalled"], on["memories_recalled"]))
    print(_row("similar incident surfaced", off["similar_incident_found"], on["similar_incident_found"]))
    print(_row("failed-action warning", off["failed_action_warning"], on["failed_action_warning"]))
    print(_row("top hypothesis", off["top_hypothesis"], on["top_hypothesis"]))
    print(_row("top hypothesis confidence", off["top_hypothesis_confidence"], on["top_hypothesis_confidence"]))
    print(_row("actions executed", off["actions_executed"], on["actions_executed"], better="down"))
    print(_row("diagnostic steps", off["diagnostic_steps"], on["diagnostic_steps"], better="down"))
    print(_row("failed remediations", off["failed_action_count"], on["failed_action_count"], better="down"))
    print(_row("repeated the known bad action", off["repeated_failed_action"], on["repeated_failed_action"], better="down"))
    print(_row("steps to confirmed cause", off["steps_to_confirmed_cause"], on["steps_to_confirmed_cause"], better="down"))
    print("-" * 72)
    print(f"verdict: {result['verdict']['headline']}")
    print(f"memory changed the recommendation: {result['verdict']['memory_changed_recommendation']}")
    print(f"steps saved: {result['verdict']['steps_saved']}")

    if args.json:
        Path(args.json).write_text(json.dumps(result, indent=2, default=str), encoding="utf-8")
        print(f"raw result written to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
