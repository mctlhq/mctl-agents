# ADR 023 — Runtime assurance probe harness

> **Status:** proposed
> **Date:** 2026-10-21
> **Issue:** mctlhq/mctl-agents#695 (epic: mctlhq/.github#151; contracts: #694, ADR 022)
> **Implements:** `orchestrator/assurance_harness.py`, `tests/test_assurance_harness.py`

## Decision

One `AssuranceScenario` runs through `assurance_harness.execute(scenario, probe,
config, seed=, attempt=)`, which returns a `HarnessReport`: a machine-readable
`failure_class` and `stop_reason`, and, once the target has been touched, the
sealed `AssuranceRun` and `AssuranceResult` from ADR 022. The contracts are
consumed unchanged, and every emitted pair passes `check_run_against_scenario`
and `check_result_against_run` before it is returned. The function never
raises.

## Who owns what

| Concern | Owner | Where it is enforced |
|---|---|---|
| Next tool and its arguments | probe (scripted now, model-driven later) | `ProbeContext.call_tool`, the only call a probe can make |
| Target, revision, transport, credential | server (`HarnessConfig.targets`) | keyed by the scenario's exact (kind, ref, environment) |
| Allowlist, enabled environments | server (`HarnessConfig`) | `_admit`, before the probe starts |
| Tools, mutation mode | scenario (reviewed data) | `_Gate.call` |
| Policy and approval | `policy_checkpoint.decide` with the configured `ApprovalLookup` | `_Gate.call`, before the transport |
| Budgets | scenario, narrowed per axis by `budget_ceiling` | `_Gate.call`, the watchdog and `_admit` (attempts) |
| Verdicts | server-registered `Detector`s over the harness transcript | `_observe` and `_check_detection` |

A `ProbeContext` holds no binding, credential, budget or policy handle
(`__slots__`). Target output is returned to the probe as `ToolOutput.text`,
which is data. The harness never parses it, and records keep only its hash.

## Refused before the target is touched (no records)

`scenario_invalid` (a parse, seal or contract failure, or a bad seed),
`environment_not_enabled` (only `fixture` is enabled by default),
`target_not_allowlisted` (a non-fixture target needs the configured allowlist and
an exact `check_target_allowlisted`), `target_not_configured`,
`action_kind_not_permitted` (the scenario declares `permitted_action_kinds`
without `mcp.tool.call`, the only kind the harness performs),
`detector_missing` (an invariant without a detector of its `check_kind`),
`attempts_exhausted`.

## Stopped during the run (records emitted)

| stop_reason | failure_class | outcome |
|---|---|---|
| `tool_not_permitted`, `request_invalid`, `reserved_argument`, `credential_in_request`, `mutation_not_permitted` | `out_of_envelope` | `refused` |
| `policy_refused` | `policy_refused` | `refused` |
| `tool_calls_exhausted`, `wall_clock_exhausted`, `output_too_large` | `budget_exhausted` | `budget_exhausted` |
| `probe_error` (any `BaseException` from the probe, `SystemExit` included) | `target_failure` | `inconclusive` |
| `harness_error` (a detector raised or answered dishonestly) | `harness_failure` | `harness_failure` |
| `completed` | `none` / `invariant_failure` / `target_failure` / `insufficient_observation` | `invariant_held` / `invariant_violated` / `inconclusive` |

- `reserved_argument`: an argument key at any depth that names credential material or harness-owned scope. Credential keys match by substring (`redaction._TOKEN_KEY` plus authorization, cookie, api key, secret, password, credential, private/session key, bearer and an `auth` segment), so `auth_token` and `x-api-key` are refused. Scope keys (`target`, `target_ref`, `environment`, `tenant`) match exactly. `redaction._DENIED_KEY` is not reused whole because it also denies ordinary argument names such as `query` and `input`.
- `credential_in_request`: a credential-shaped value or key (`redaction.contains_credential`).
- `mutation_not_permitted`: any non-read tool unless the scenario is `fixture_only` **and** the target is a fixture. So a sandbox is never mutated, and production mutation stays unrepresentable (ADR 022).

A run that stops early reports every verdict `unknown` and caps its observations
at `partial`. A completed run is `invariant_held` only when every detector saw a
full read. A failed or oversized read gives `partial` or `observation_failed`,
and no call at all gives `not_attempted`. All of these are `unknown`, never
absence. The first stop wins and is recorded harness-side. The probe gets
`_HarnessStop`, a `BaseException`, and any later call raises it again before
policy or transport are reached, so swallowing it changes nothing.

If a run that already stopped then overruns its budget (a probe that swallows a
refusal and hangs), the first stop stays the `stop_reason` and `failure_class`,
and the outcome becomes `budget_exhausted`, the only outcome ADR 022 admits for
consumption above the grant. The records are still emitted.

## Bounds

- Tool calls are counted per request admitted to the policy checkpoint. The slot is claimed under the gate's lock, so concurrent probe threads get unique call indexes and can never exceed `max_tool_calls`.
- A call handed to the transport is recorded at once as `abandoned`, and replaced when it returns. A call the watchdog leaves behind therefore counts as executed: it reached the target, so its effect is unknown, not absent.
- Wall clock uses the injected `Clock`, checked before and after every call. A call that overruns has its output withheld.
- A real-time watchdog thread stops a probe or transport that never returns, after the granted wall clock plus 1 s. The thread cannot be killed: it is abandoned, and the gate refuses anything further from it.
- Output is limited to 64 KiB per call by default, and requests to 16 KiB.
- Attempts are limited by `granted.max_attempts`. `execute_attempts` retries only `target_failure`.

## Determinism

`runtime_execution_id` is `ex-` plus 16 hex of
`hash(scenario content_hash, target revision, seed, attempt)`. With a
`ManualClock` the same inputs give byte-identical records. `started_at` and
`ended_at` come from the clock and are outside the content hash, so the
wall-clock date never changes `run_id` or `result_id`. The default approval
lookup is `NO_APPROVALS`, never `configured_approvals()`, so the environment
cannot change a decision.

## Detectors

`NoCredentialInOutput` (`deterministic_check`) and `PolicyDecisionPerCall`
(`policy_decision_expected`) ship with the harness. Each has tests for both
sides: green on a converged input, red on a mutation, and unknown when it could
not observe. `_check_detection` turns a dishonest answer into a
`harness_failure`. A dishonest answer is a decided verdict without an
`observed` status, an `observed` status without a decision, an uncited decision,
or a citation of a call that does not exist.

## Not done here (follow-ups)

- **Decision records.** The harness keeps the `PolicyDecisionRef` of each call in `AssuranceRun.policy_decisions`. Durable decision records (mctl-api#701) come from `policy_checkpoint` once that producer lands, with no harness change.
- **`ApprovalRef`.** `AssuranceRun.approvals` stays empty: `Decision` carries `approval_ref` but not the intent hash an `ApprovalRef` needs.
- **Evidence ids.** Observations cite a transcript `ArtifactRef`, not `ev-` evidence ids. Sealing the transcript as `ExecutionEvidence` is a later step.
- Follow-up execution (`bounded_adaptive`), the adversarial boundary suite, and any runner, CLI or Temporal wiring.
