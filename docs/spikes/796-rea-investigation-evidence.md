# Spike #796: REA as an investigation evidence provider

STATUS: DRAFT -- no recorded live measurement run backs this report yet, so it
carries no decision token. The corpus, harness and guards are delivered; the
measurements come from an operator run (same split as
`docs/benchmarks/capability-discovery.md`). Refs mctlhq/mctl-agents#796.

Allowed decision tokens, one of which replaces this marker when the report is
finalized: `ADOPT_OPTIONAL_REA_CAPABILITY`, `ADOPT_REA_CLI_ADAPTER_ONLY`,
`RESEARCH_ONLY`, `REJECT`. The issue owner records the verdict.

## Environment and versions

See `docs/spikes/796/rea-version.md` (REA SHA, version, install method, date:
all `unobserved` until the live run). Claude path: re-read
`ISSUE_INVESTIGATOR_MODEL` from `cwft-mctl-agents-investigate.yaml` at run time
(observed as `claude-opus-5-5` when the proposal was written). SDK version:
unobserved.

## Current-state map

| Issue | Relation | Status |
| --- | --- | --- |
| #242 capability discovery | `orchestrator/capability_gateway.py` fronts only the mctl provider; no REA provider | not duplicated |
| #597 context efficiency | `docs/benchmarks/capability-discovery.md`, `tools/capability_bench.py` counting rule | reused (schema-bytes rule) |
| #746 portable runtime | `docs/spikes/746-portable-runtime.md` | not duplicated |
| #791 model/provider benchmark | no open-model provider found in `orchestrator/` or `config/model-policy.yaml` | unknown; matrix rows are `not-run` unless an endpoint is supplied |

## Corpus and oracle

`docs/spikes/796/fixtures/`: `native-codec` (C, stripped) and `packaged-app`
(JVM jar fallback; no JDK/Android SDK was available to the implementer, so the
artefact kind actually used is `unobserved`). Each has `oracle.yaml`,
`task.md` and `build.sh`. Built artefacts are not committed, only digests
(`unobserved` until built). No auth, licence, DRM or anti-tamper logic.

## Metrics table

| fixture | arm | oracle facts | unsupported | grounded | turns | tool calls | in tokens | out tokens | wall s | actionable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| native-codec | baseline | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |
| native-codec | rea-mcp | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |
| native-codec | rea-cli | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |
| packaged-app | baseline | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |
| packaged-app | rea-mcp | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |
| packaged-app | rea-cli | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved | unobserved |

Grading is human (grader: unobserved), n=1 per arm.

## Claude stage-by-stage result

Five stages, one attempt each, no retry or rephrase. Failure classes:
`MODEL_POLICY_REFUSAL`, `CLIENT_TOOL_ADMISSION`, `MCP_SCHEMA_OR_CONTEXT_FAILURE`,
`REA_TOOL_FAILURE`, `PROVIDER_RUNTIME_FAILURE`, `UNKNOWN`. Result: unobserved.

## Cross-model matrix

| model | 1-discovery | 2-static | 3-trace | 4-synthesis | 5-implement |
| --- | --- | --- | --- | --- | --- |
| kimi-k2.7-code | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) |
| deepseek-v4-flash | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) |
| glm-5.2 | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) |
| qwen-3.6 | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) | not-run (no #791 endpoint supplied) |

## Schema footprint

REA `tools/list` count and serialized schema bytes: unobserved. Whether ADR 017
discovery could hide them: today it serves only the mctl provider, so it would
need a second provider (on paper only; not built here).

## Direct MCP vs CLI adapter vs none

Failure behaviour when REA is absent or misconfigured, per shape: unobserved.

## Evidence-contract mapping (ADR 009 / ADR 018)

Candidate mapping onto existing primitives, to be confirmed against one real
REA result per fixture:

- target identity: `ContextSource.locator` + `content_hash` (`fixture://<name>@<sha256>`)
- operation: `ToolCallRef.name` / `status`
- observed vs inferred: `Provenance.authority` `observed` vs `derived`
- limitation or unknown: `Gap(observation_failed)`
- trust: `Trust.tier=reported`

Possible gap to confirm or refute: no `SOURCE_KINDS` member for a local
analysis result and no `SUBJECT_KINDS` member for a binary target. Decision:
unobserved (reuse existing primitives, or name the smallest extension).

## Setup, data and safety note

Only MCTL-owned, source-built fixtures are analysed and sent to a model
provider; the owner should confirm this against the MCTL data policy (none was
found in this repo). Raw REA output is capped at 4 KiB plus sha256 and binaries
are never committed. A refusal is recorded and the stage stops; it is never
worked around.

## Decision

Pending the operator run. Follow-up issues: none until the experiment proves a gap.
