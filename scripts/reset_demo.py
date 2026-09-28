"""Reset the demo to a known state.

    python scripts/reset_demo.py            # reset + re-seed INC-A1
    python scripts/reset_demo.py --empty    # reset only, create nothing
    python scripts/reset_demo.py --keep-memory
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from recallops.api.deps import build_container, get_container, set_container  # noqa: E402
from recallops.persistence.db import reset_db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Reset the RecallOps demo")
    parser.add_argument("--empty", action="store_true", help="do not re-create the seed incident")
    parser.add_argument("--keep-memory", action="store_true", help="keep organisational memory (Hindsight + mirror)")
    args = parser.parse_args()

    reset_db()
    set_container(build_container())
    container = get_container()

    if not args.keep_memory:
        try:
            container.memory.reset()
        except Exception as exc:  # noqa: BLE001
            print(f"warning: could not clear the memory layer: {exc}")

    created = None
    if not args.empty:
        created = container.orchestrator.create_incident("INC-A1")["incident"]["id"]

    print(
        json.dumps(
            {
                "status": "reset",
                "seeded_incident": created,
                "scenarios": container.orchestrator.scenarios.ids(),
                "memory_cleared": not args.keep_memory,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
