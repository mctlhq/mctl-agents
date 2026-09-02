#!/usr/bin/env python3
"""Record workflow event histories into `tests/fixtures/histories/` (#251).

    uv run python tools/record_workflow_history.py --kind patched
    uv run python tools/record_workflow_history.py --kind patched dev_loop_full

Run by hand, deliberately, and commit the result. This is NOT wired into CI:
the value of a recorded history is that it is OLD — it is evidence about what
a previous version of the code scheduled. A job that regenerates it on every
run would rewrite that evidence to agree with whatever is on the branch, and
`tests/test_workflow_replay.py` would pass forever without testing anything.

`--kind` is required and has no default, deliberately. A `*.prepatch.json`
is evidence about what an OLDER version of the code scheduled and is the only
thing exercising the unpatched branch of a marker; re-recording it after the
change it guards destroys that evidence while every test keeps passing. There
is no default that is safe for both kinds, so the caller has to say which one
they mean. The replay tests additionally assert on fixture content, so an
accidental re-record fails loudly rather than quietly.

Needs the Temporal time-skipping test server, which temporalio downloads and
caches under ~/.cache on first use — so the first run wants network.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from temporalio.testing import WorkflowEnvironment  # noqa: E402

from tests.replay_scenarios import (  # noqa: E402
    HISTORY_DIR,
    SCENARIOS,
    Scenario,
    record,
    scenario_by_name,
)


async def _record_one(scenario: Scenario, kind: str) -> None:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        handle = await record(env.client, scenario)
        history = await handle.fetch_history()

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    path = scenario.path_for(kind)
    payload = history.to_json_dict()
    # sort_keys + trailing newline so a re-record produces a reviewable diff
    # rather than a reshuffle of the whole file.
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(f"wrote {path.relative_to(ROOT)}  ({len(payload.get('events', []))} events)")
    if scenario.covers:
        print(f"       covers: {scenario.covers}")


async def _main(kind: str, names: list[str]) -> int:
    scenarios = [scenario_by_name(n) for n in names] if names else list(SCENARIOS)
    for scenario in scenarios:
        await _record_one(scenario, kind)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kind",
        required=True,
        choices=("prepatch", "patched"),
        help="prepatch = before a guarded change (evidence; do not re-record); "
        "patched = after it",
    )
    parser.add_argument("scenarios", nargs="*", help="scenario names; default all")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(_main(args.kind, args.scenarios)))
