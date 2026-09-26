# Capability discovery vs eager loading — benchmark

**Status: measured 2026-09-26, one run per mode.** One fixed issue and one
target SHA. This is a single paired observation, not a statistical claim.

The comparison is between two ways the issue-investigator reaches the mctl
tools (mctlhq/mctl-agents#242, ADR 017):

- `eager` is today's path: the model connects the remote mctl MCP server
  directly and sees every `mcp__mctl__*` schema;
- `discovery`: the model sees three gateway tools, `capability_search`,
  `capability_describe` and `capability_invoke`, over one sealed
  `CapabilitySet`.

The acceptance criterion from requirements.md is: "for one fixed issue and
target SHA, the count and serialized byte size of tool schemas present in
the initial model context and the end-to-end input/output token usage, in
both modes, with the sample size stated."

## Setup

| | |
| --- | --- |
| Issue | mctlhq/mctl-api#388 (re-initialise failed stores in the background) |
| Target SHA | `f9bbdc1b559437e56f56ccd8c45947963517e4cc` (mctl-api `main`, same for both runs) |
| Agent code | mctl-agents `main` at `aeba0f9` |
| Catalog | mctl-gitops `main` after #1425: profile `issue-investigator-default` 1.5.0, `capabilityDiscovery.enabled: true` |
| Model / budget | `claude-opus-5`, $8.00 (the investigate CWFT's values) |
| Modes | `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative` in both runs; only `ISSUE_INVESTIGATOR_CAPABILITY_MODE` differs (`eager` / `discovery`) |
| Where | Operator-run, outside the cluster, back to back. Each run used its own scratch copy of `agents-state`. A `gh` shim blocked writes, so neither run posted to the issue, and neither run pushed its proposal. |

Both runs finished `success` and wrote a complete proposal triplet.

## Initial tool-schema context (`schema-bytes`)

`tools/capability_bench.py schema-bytes`, one live listing of the mctl-api
provider:

| | Eager | Discovery |
| --- | --- | --- |
| Tool schemas in the first turn | 92 | 3 |
| Serialized schema bytes (`inputSchema` only) | 40,313 | 371 |

The first model call of each run shows what that is worth in tokens. The
first turn is written to cache, so it appears as the first
`AssistantMessage`'s `cache_creation_input_tokens`:

| | Eager | Discovery | Difference |
| --- | --- | --- | --- |
| First-turn prompt (tokens) | 61,213 | 23,392 | −37,821 (−62%) |

This number is larger than the byte count because the eager path also
carries every tool's name and description, not only its input schema.

## End to end (`compare`)

`tools/capability_bench.py compare eager.json discovery.json`, over each
run's captured `ResultMessage`s:

| Metric | Eager | Discovery |
| --- | --- | --- |
| Input tokens | 2055 | 2421 |
| Output tokens | 51520 | 74187 |
| Cache-read tokens | 3431455 | 4679919 |
| Cache-creation tokens | 268192 | 232745 |
| Turns | 30 | 52 |
| Wall clock (API, ms) | 768861 | 1301397 |

Sample size: 1 run per mode.

Token totals include the CLI's small `claude-haiku-4-5` side calls (about
2k input tokens per run). The CLI reported this cost for each run, and the
harness does not compute USD: eager $5.24, discovery $6.10.

## Reading the result

- **The discovery path does what it claims on the first turn.** The initial
  context shrinks by about 38k tokens (62%). Every later turn re-reads that
  prefix from cache, so on a run with the same number of turns the saving
  compounds through cache reads.
- **This pair does not show an end-to-end saving.** The discovery run took
  52 turns against eager's 30, and it wrote a longer proposal (6,887 words
  against 5,653). The extra turns outweigh the smaller prefix: cache reads
  rose 36%, output rose 44%, and cost rose 17%.
- **Neither run actually used an mctl tool.** Eager made no `mcp__mctl__*`
  call. Discovery made two `capability_search` calls ("readiness probe pod
  health store initialization", "github issue design proposal lifecycle")
  and no `capability_invoke`. So the turn gap is not the cost of going
  through the gateway. With n = 1 it is indistinguishable from ordinary
  run-to-run variance in how long an agent explores a repository.
- **What this supports:** the first-turn reduction is a structural property
  of the mode. It will hold on every run.
- **What it does not support:** a claim that discovery lowers end-to-end cost
  or turns. That needs several runs per mode, ideally on an issue where the
  agent does call mctl tools, so that gateway round-trips are measured
  rather than assumed.

`duration_api_ms` and `num_turns` are summed per session as cumulative
values (see `tools/capability_bench.py`). Each run here produced exactly one
`ResultMessage`, so that assumption did not affect these numbers.

## How to reproduce

1. The catalog must permit discovery (`capabilityDiscovery.enabled: true`
   on `issue-investigator-default`). This has been the case on mctl-gitops
   `main` since #1425.
2. Measure the schema bytes (live, no model run):

   ```
   MCTL_TOKEN=... uv run python tools/capability_bench.py schema-bytes
   ```

3. Run the investigator on the same issue once per mode, back to back.
   Before the second run, confirm that the target repository's `main` has
   not moved: the investigator clones its HEAD. Use this environment for
   both runs:

   - `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`
   - `ISSUE_INVESTIGATOR_CAPABILITY_MODE=eager|discovery`
   - `MCTL_GITOPS_ROOT` pointing at a mctl-gitops checkout
   - `--state-dir` pointing at a scratch copy of `agents-state`

   Capture every `ResultMessage` as JSON. Wrapping
   `usage_ledger.UsageRecorder.observe` is enough, because the investigator
   routes every SDK message through it. Record at least `session_id`,
   `uuid`, `model_usage`, `num_turns`, `duration_api_ms` and `is_error`.
4. Render the table:

   ```
   uv run python tools/capability_bench.py compare eager.json discovery.json
   ```
