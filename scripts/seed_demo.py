"""Seed the demo: create every seeded incident so the console has data to show.

    python scripts/seed_demo.py            # create incidents (idempotent)
    python scripts/seed_demo.py --all      # also run the full scripted demo
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recallops.api.deps import get_container  # noqa: E402
from recallops.persistence.db import init_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Seed RecallOps demo data")
    parser.add_argument("--all", action="store_true", help="also run the full scripted demo (A1 -> A2 -> comparison)")
    parser.add_argument("--story", action="store_true", help="play the full incident story (default when the database is empty)")
    parser.add_argument("--reset", action="store_true", help="reset the database first")
    parser.add_argument("--reset-only", action="store_true", help="do not play the story, just create NEW incidents")
    args = parser.parse_args()

    init_db()
    container = get_container()
    orchestrator = container.orchestrator

    if args.reset:
        from recallops.persistence.db import reset_db

        reset_db()
        from recallops.api.deps import build_container, set_container

        set_container(build_container())
        container = get_container()
        orchestrator = container.orchestrator

    from recallops.services.seed_story import is_empty, seed_story

    if args.story or (is_empty(orchestrator) and not args.reset_only):
        story = seed_story(orchestrator=orchestrator, memory=container.memory)
        print(json.dumps(story, indent=2, default=str))
        return 0

    created = []
    for scenario in orchestrator.scenarios:
        result = orchestrator.create_incident(scenario.id)
        created.append({"incident": result["incident"]["id"], "created": result["created"]})
    print(json.dumps({"seeded": created}, indent=2))

    if args.all:
        from recallops.services.demo import DemoService

        service = DemoService(orchestrator=orchestrator, memory=container.memory, session_factory=orchestrator.session)
        story = asyncio.run(service.run_scripted_demo(["INC-A1", "INC-A2"], run_comparison=True))
        print(json.dumps(story["summary"], indent=2))
        if story.get("comparison"):
            print(json.dumps(story["comparison"]["delta"], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
