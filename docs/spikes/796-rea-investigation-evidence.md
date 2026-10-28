# Spike #796: REA as an investigation evidence provider

STATUS: REJECT

`REJECT` is the recommended token from the measurements below. The issue owner
records the verdict in `.github/roadmap/spikes/rea-investigation-evidence.yaml`;
this report changes only if the owner decides otherwise. Refs
mctlhq/mctl-agents#796.

Allowed tokens were `ADOPT_OPTIONAL_REA_CAPABILITY`,
`ADOPT_REA_CLI_ADAPTER_ONLY`, `RESEARCH_ONLY` and `REJECT`.

## Summary

On both fixtures, giving the investigator REA (as an MCP server or as a CLI on
`PATH`) changed nothing in the answer: the same oracle facts were recovered in
every arm, no final claim cited a REA result, and in the scored run the model
made **zero** REA calls in all six investigations. The only cost REA added was
context: its MCP server exposes 229 tools in 121,537 schema bytes, which raised
total input tokens by up to 48% where the schema was loaded. The one stage that
did call REA (probe, native-codec, `2-static`) failed. The corpus is easy
enough that the model reached the ceiling without help, so REA's value on a
harder target is untested; that is a reason to re-open the question with a
harder corpus, not to integrate REA now. A second model, Qwen3.6 on Hetzner
inference, showed the same pattern on packaged-app (no REA calls, same
accuracy, 14x input tokens with the MCP schema) and did not finish
native-codec in any arm.

## Environment and versions

- REA: `rea-agents` 6.8.0, commit `160d821ac9a0310afcb8d1fc427862dd6508add5`,
  bound by npm SLSA provenance; details in `docs/spikes/796/rea-version.md`.
- Claude path: `claude-opus-5-5`, re-read from `ISSUE_INVESTIGATOR_MODEL` in
  `cwft-mctl-agents-investigate.yaml` at run time
  (`model_matches_investigator: true`); claude-agent-sdk 0.2.168, Claude Code
  CLI 2.1.301, mcp 1.29.0, Python 3.12.3, GitHub-hosted Linux runner.
- Runs (workflow `rea-spike-796`, one attempt per cell, no retries):
  - [40525903080](https://github.com/mctlhq/mctl-agents/actions/runs/40525903080)
    at `main` before #841: every REA cell `rea-unavailable` (npm records no
    `gitHead` for this OIDC-published package). Baselines only.
  - [40529056591](https://github.com/mctlhq/mctl-agents/actions/runs/40529056591)
    at `c1d540b` (#841: provenance binding, classifier counter).
  - [40533093166](https://github.com/mctlhq/mctl-agents/actions/runs/40533093166)
    at `7afb04b` (#843: REA call counting). **Scored run.**
- Bundles (records, metadata, schema bytes; no binaries) are committed under
  `docs/spikes/796/results/run-<id>/`; human grading of the scored run is in
  `run-40533093166/grading.yaml`.

## Current-state map

| Issue | Relation | Status |
| --- | --- | --- |
| #242 capability discovery | `orchestrator/capability_gateway.py` fronts only the mctl provider; no REA provider | not duplicated |
| #597 context efficiency | `tools/capability_bench.py` counting rule | reused for schema bytes |
| #746 portable runtime | `docs/spikes/746-portable-runtime.md` | not duplicated |
| #791 model/provider benchmark | Hetzner inference serves `Qwen/Qwen3.6-35B-A3B-FP8` through an Anthropic-compatible endpoint; Kimi, DeepSeek and GLM are not in that catalog | used for the Qwen row |

## Corpus and oracle

`docs/spikes/796/fixtures/`: `native-codec` (C, `-O2`, stripped, 6 facts
N1-N6) and `packaged-app` (JVM jar fallback built with JDK 17; no Android SDK,
5 facts P1-P5). Built artefacts are not committed; digests are in each run's
`fixtures.json`. No auth, licence, DRM or anti-tamper logic.

## Metrics table (scored run 40533093166, claude-opus-5-5)

`in tokens` is the total prompt size: `input_tokens` plus cache creation plus
cache read. The harness's bare `input_tokens` field excludes cache and is not
comparable across arms.

| fixture | arm | oracle facts | unsupported | cites REA | REA calls | turns | tool calls | in tokens | out tokens | wall s | actionable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| native-codec | baseline | 4/6 | 0 | 0 | 0 | 7 | 6 | 144,812 | 5,591 | 76.7 | yes |
| native-codec | rea-mcp | 4/6 | 0 | 0 | 0 | 7 | 6 | 143,321 | 4,973 | 51.1 | yes |
| native-codec | rea-cli | 4/6 | 0 | 0 | 0 | 6 | 5 | 92,507 | 4,715 | 51.6 | yes |
| packaged-app | baseline | 5/5 | 0 | 0 | 0 | 4 | 3 | 83,482 | 2,988 | 34.9 | yes |
| packaged-app | rea-mcp | 5/5 | 0 | 0 | 0 | 6 | 5 | 123,418 | 3,288 | 38.0 | yes |
| packaged-app | rea-cli | 5/5 | 0 | 0 | 0 | 4 | 3 | 83,271 | 2,735 | 35.3 | yes |

Run 40529056591, same model and REA, before call counting existed (accuracy
identical; answers cite `objdump`/`javap`, never REA):

| fixture | baseline | rea-mcp | rea-cli |
| --- | --- | --- | --- |
| native-codec in tokens | 114,412 | 166,218 | 132,322 |
| packaged-app in tokens | 115,048 | 161,858 | 83,430 |

Grading is by the operator session for mashkovd, n=1 per arm. native-codec
N3/N4 (seed and table) are stated in the compiler-folded form (seed
`0x6f8cb43d` with byte 0 skipped, which equals `rotl32(0x9e3779b1, 3) ^
0x9e3779b1` because byte 0 is the constant `0x00`). The model the answer builds
is byte-identical to the binary on differential runs, but N3/N4 are graded
strictly as not recovered, identically in every arm, so the comparison is
unaffected.

Wall-clock and token variance between runs 2 and 3 on the same arm (for
example native-codec baseline 114k vs 145k) is as large as the variance
between arms, so no arm is faster or cheaper beyond the MCP schema load.

## Claude stage-by-stage result

Five-stage probe, `rea-mcp` arm, one attempt per stage, no retry or rephrase.

| run | fixture | 1-discovery | 2-static | 3-trace | 4-synthesis | 5-implement |
| --- | --- | --- | --- | --- | --- | --- |
| 40529056591 | native-codec | ok (classifier 1) | ok | ok | ok | ok |
| 40529056591 | packaged-app | ok | ok | ok | ok | ok |
| 40533093166 | native-codec | ok (classifier 1, REA 0) | **failed: `REA_TOOL_FAILURE`** (REA 4, classifier 1) | not-run | not-run | not-run |
| 40533093166 | packaged-app | ok | ok | ok | ok | ok |

No stage, run or arm produced `MODEL_POLICY_REFUSAL`. The failed `2-static`
stage is the only place REA was actually called: one of its four REA calls
returned an error, and the same stage was cut by the client safety classifier
(below). The identical probe passed all five stages one run earlier.

### Client safety-classifier interruptions

Claude Code injects the notice "Your response above was stopped by a safety
classifier -- this is not a tool or API error" as text inside the turn. It is
not a `refusal` stop reason, so before #841 the harness recorded such sessions
as plain `ok`. Counted by `classifier_interruptions` since #841:

| fixture | sessions | interrupted | with REA | without REA |
| --- | --- | --- | --- | --- |
| native-codec | 14 | 7 | 5 of 11 | 2 of 3 |
| packaged-app | 17 | 0 | 0 | 0 |

(Run 1's native-codec baseline interruption was read from its bounded summary;
runs 2 and 3 are counted by the harness. Sessions = investigation runs plus
probe stages that ran.) The rate does not depend on REA; it depends on the
fixture (a stripped native binary). Every interrupted session recovered and
finished except the failed probe stage above. `orchestrator/` has no detection
for this notice today, so a production investigation cut this way would look
like an ordinary answer; that is filed as #849, independent of REA.

## Cross-model matrix

| model | result |
| --- | --- |
| claude-opus-5-5 | measured (above) |
| qwen-3.6 (`Qwen/Qwen3.6-35B-A3B-FP8`, Hetzner #791 endpoint) | measured: packaged-app all arms; native-codec not finished within 1200 s in any arm |
| kimi-k2.7-code | not-run: not in the Hetzner inference catalog (#791) |
| deepseek-v4-flash | not-run: not in the Hetzner inference catalog (#791) |
| glm-5.2 | not-run: not in the Hetzner inference catalog (#791) |

### Qwen row (Hetzner inference)

All Qwen numbers come from Hetzner inference; no other provider was used.
Same harness commit, REA install and fixtures as the scored run, executed in a
workstation container because the workflow has no alternate-provider input.
Setup, limits and grading are in `docs/spikes/796/results/qwen-hetzner-2026-10-27/`.

| fixture | arm | oracle facts | unsupported | cites REA | REA calls | turns | tool calls | in tokens | out tokens | wall s | actionable |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| native-codec | baseline | not finished within 1200 s | | | 0 | | | | | 1200 | no |
| native-codec | rea-mcp | not finished within 1200 s | | | 0 | | | | | 1200 | no |
| native-codec | rea-cli | not finished within 1200 s | | | 0 | | | | | 1200 | no |
| packaged-app | baseline | 5/5 | 1 | 0 | 0 | 8 | 7 | 79,225 | 3,647 | 61.1 | yes |
| packaged-app | rea-mcp | 5/5 | 0 | 0 | 0 | 14 | 13 | 1,115,484 | 5,661 | 204.2 | yes |
| packaged-app | rea-cli | 5/5 | 1 | 0 | 0 | 13 | 12 | 167,260 | 8,489 | 162.2 | yes |

Stage probe (`rea-mcp`): packaged-app failed at `1-discovery` with
`REA_TOOL_FAILURE` (one REA call, which errored); native-codec did not finish
within 3000 s. No `MODEL_POLICY_REFUSAL` and no classifier interruption.

The Qwen row agrees with the Claude row: REA was available and never used in an
investigation, and accuracy was the same with and without it. The schema cost
is much larger here: 14x the baseline's total input tokens on `rea-mcp`, because
the 121 KB schema is resent on every turn and Qwen took more turns. native-codec
is out of reach for this model at this budget regardless of REA.

Running Claude Code against a non-Anthropic gateway needed
`ANTHROPIC_DEFAULT_SONNET_MODEL` set to the gateway's model. Without it, the CLI's
auto-mode permission classifier calls `claude-sonnet-5` on the same base URL,
gets an error, denies every compound Bash command, and the session loops
without end. This matters to #791 and to any non-Anthropic investigator route,
independent of REA.

## Schema footprint

REA's MCP server answers `tools/list` with **229 tools, 121,537 serialized
schema bytes** (`schema-bytes.json`, counting rule from #597). The `rea-mcp`
arm pays this on every session whether or not REA is used: +41% (run 2) and
+48% (run 3) total input tokens on packaged-app, +45% on native-codec in run 2;
native-codec in run 3 came out 1% below baseline, inside run-to-run noise.
ADR 017 discovery could hide the schema only after a second provider is added
to `capability_gateway.py`; nothing measured here justifies building it.

## Direct MCP vs CLI adapter vs none

- **None (baseline):** same accuracy, no extra context.
- **CLI on `PATH`:** no schema cost, but in the scored run the model never
  invoked `rea`; it reached for `objdump`, `strings`, `javap` and its own
  differential tests.
- **Direct MCP:** pays 121 KB of schema per session; zero calls in the six
  scored investigations; four calls in one probe stage, which failed.
- **REA absent or misconfigured:** run 40525903080 shows the fail-closed path.
  The workflow refused to start any REA cell when it could not bind the
  installed package to the resolved commit, and recorded each as
  `infra-failure` / `rea-unavailable` rather than silently running the arm
  without REA.

## Evidence-contract mapping (ADR 009 / ADR 018)

Decision: **reuse existing primitives; no extension.** No investigation
produced a REA evidence item, so there was nothing to persist and no gap in
`SOURCE_KINDS`/`SUBJECT_KINDS` was exercised. The candidate mapping stays on
paper for a future re-open:

- target identity: `ContextSource.locator` + `content_hash` (`fixture://<name>@<sha256>`)
- operation: `ToolCallRef.name` / `status`
- observed vs inferred: `Provenance.authority` `observed` vs `derived`
- limitation or unknown: `Gap(observation_failed)`
- trust: `Trust.tier=reported`

## Setup, data and safety note

Only MCTL-owned, source-built fixtures were analysed and sent to a model
provider (Anthropic for the Claude rows, Hetzner inference for the Qwen row).
Raw REA output was capped at 4 KiB plus sha256 and no binary is committed. No
refusal or classifier interruption was worked around; each attempt ran once.

## Decision

Recommended: `REJECT` for DevLoop integration in any shape (MCP, CLI adapter or
optional capability). REA added context cost and one tool failure and changed
no answer. Re-opening needs a corpus the baseline cannot solve (for example an
obfuscated or packed target where `objdump`/`javap` are not enough), measured
the same way.

Follow-ups:

- Investigator: detect and record Claude Code safety-classifier interruptions
  instead of treating the turn as a normal answer (#849).
- Remove the spike workflow `rea-spike-796.yml`, `tools/rea_spike.py` and this
  spike's guard test once the verdict is recorded (#850).
