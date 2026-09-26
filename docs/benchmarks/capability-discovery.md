# Capability discovery vs eager loading — benchmark

**Status: not yet measured.**

This document will hold the measured comparison between `eager` (today's
path: the model connects the remote mctl MCP server directly) and
`discovery` (mctlhq/mctl-agents#242, ADR 017: the model calls
`capability_search`/`capability_describe`/`capability_invoke` over one
sealed `CapabilitySet` instead) for the issue-investigator, per
requirements.md's benchmark acceptance criterion: "for one fixed issue and
target SHA, the count and serialized byte size of tool schemas present in
the initial model context and the end-to-end input/output token usage, in
both modes, with the sample size stated."

## Why this is blank

The harness (`tools/capability_bench.py`) is implemented and unit-tested
(`tests/test_capability_bench.py`) with no model call and no network. Filling
in the numbers below needs two things this implementer pass cannot do:

1. **The mctl-gitops catalog field** (`spec.capabilityDiscovery`) on
   `issue-investigator-default`, currently `enabled: false` by default — a
   human-authored mctl-gitops PR, landed after this proposal's mctl-agents
   half merges (part B, per this proposal's tasks.md).
2. **A live paid run** in each mode, for the same fixed issue and target SHA
   (part C, an operator step, after part B flips `enabled: true` for the
   measurement).

## How to run the measurement (part C, operator)

1. In a reviewed mctl-gitops PR, set `issue-investigator-default`'s
   `spec.capabilityDiscovery: {enabled: true, providers: [...]}` (see
   design.md sec. 1 for the field shape). Merge it.
2. `schema-bytes` — one live call, no model run needed, reports the
   first-turn tool-schema byte counts for both modes:

   ```
   MCTL_TOKEN=... uv run python tools/capability_bench.py schema-bytes
   ```

3. Run the issue-investigator once against a fixed issue and target SHA with
   `ISSUE_INVESTIGATOR_CAPABILITY_MODE` unset (or `eager`), and once more,
   same issue and SHA, with `ISSUE_INVESTIGATOR_CAPABILITY_MODE=discovery`
   and `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`. Capture each run's
   `ResultMessage`s as a JSON array (one object per message, with at least
   `session_id`, `model_usage`, `uuid`, `num_turns`, `duration_api_ms`,
   `is_error` — the fields `orchestrator.usage_ledger.UsageRecorder` reads).
4. Render the table:

   ```
   uv run python tools/capability_bench.py compare eager.json discovery.json
   ```

5. Paste both outputs below, replacing this section, and state the sample
   size explicitly (1 run per mode for this pilot — a single fixed issue and
   target SHA, not a statistical claim).

USD cost is not computed by the harness: the price catalog is server-side
(mctl-api), not duplicated in this repository.

## Results

_Not yet measured._
