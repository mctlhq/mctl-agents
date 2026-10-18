# ADR 022 — Runtime assurance contracts (`AssuranceScenario`, `AssuranceRun`, `AssuranceResult`, `AssuranceFinding`)

> **Status:** proposed
> **Date:** 2026-10-18
> **Issue:** mctlhq/mctl-agents#694 (epic: mctlhq/.github#151)
> **Implements:** `orchestrator/runtime_assurance.py` (schema and pure checks only),
> `tests/test_runtime_assurance.py`, `tests/fixtures/assurance/`

## Scope and non-goals

`assurance.mctl.ai/v1alpha1` defines the data a later deterministic, bounded
probe harness consumes: a scenario, the allowlist that scopes it, a run of the
scenario, the run's result, and the finding lifecycle that may follow.

Non-goals: executing probes or any runner / CLI / Temporal wiring; adaptive
scenario generation; persistence (Tier B, mctl-api); a second policy engine,
approval store or evidence store; scanning public or production targets;
production mutation of any kind.

The module is pure and stdlib-only (plus the repo's hashing rule, credential
screen, and the `execution_evidence` / `policy_checkpoint` public names). It
defines no field that expresses allow, deny, grant, approval or credential
material.

## Records

| Kind | Id | Key |
|---|---|---|
| `AssuranceScenario` | `asc-<16hex>` | `(name, version)`; bound exactly by `scenario_id` + `content_hash` |
| `AssuranceTargetAllowlist` | `atl-<16hex>` | `(name, version)`; entries hashed individually |
| `AssuranceRun` | `arn-<16hex>` | one attempt of one scenario |
| `AssuranceResult` | `ars-<16hex>` | one per run |
| `AssuranceFinding` | `afd-<16hex>` | one revision per state change |

Every record is a frozen dataclass. `content_hash` is
`context_snapshot.hash_bytes(canonical_json(hashed fields))`; the id is the
prefix plus the first 16 hex of that hash. Parsing recomputes both and rejects a
mismatch, an unknown `api_version`, a mismatched `kind` and any unknown key.
`started_at` and `ended_at` are not hashed.

## Trust boundaries and the validator rule that enforces each

1. **Scope is data, not prompt.** Target, allowlist entry, mutation mode, action kinds, tools and budgets are validated fields of the scenario. A model cannot author them; its only authored shapes are `FollowupProposal` and finding text.
2. **Exact allowlisting.** `shadow` / `production` targets require an `allowlist_entry`; `fixture` forbids one. `check_target_allowlisted` succeeds only for the exact allowlist id (version), entry hash, kind, ref and environment. Wildcards and globs are refused in every ref. The check narrows scope; it never authorizes an action.
3. **Mutation is explicit and closed.** `mutation_mode` is `none` (default) or `fixture_only`. Mutating action kinds (every `policy_checkpoint.ACTION_KINDS` member except `mcp.tool.call`) require a non-`none` mode. `production` with any mode but `none` is refused: production mutation is unrepresentable in v1alpha1. A run must repeat its scenario's mode.
4. **Budgets are required and capped.** `max_attempts` 1-10, `max_tool_calls` 1-256, `max_wall_clock_seconds` 1-3600, `max_followups` 0-16, all mandatory. A run's granted budget must be within the scenario's; `consumed` must be within the granted budget unless the outcome is `budget_exhausted`. `deterministic` scenarios allow no follow-ups.
5. **Bounded follow-ups.** `validate_followup` accepts only a declared template with exactly its declared parameters, each within its `ParamBound`. `FollowupProposal` has no target, tenant, credential, tool or action-kind field.
6. **Exact version binding.** Run, result and finding carry a `VersionBinding`: scenario id / hash / name / version, `VersionPins` (agent, environment, definition version and content hash, profile name and version together) and target kind / ref / revision (full git SHA or bounded version token). Blank or malformed fields are refused. A result and a finding must equal the binding of the run they describe.
7. **Honest observation.** Every `ObservationRef` has a status (`observed`, `partial`, `observation_failed`, `not_attempted`); `observed` requires an `ev-` id or artifact hash. `check_result_against_run` refuses `held` on anything but observed support, `invariant_held` with any non-`held` verdict, and `invariant_violated` without an observed violated verdict. A failed or partial observation is `unknown`, never absence.
8. **Model claims are not ground truth.** A finding is `candidate` until `transition_finding` confirms it with a replay: a different run, relation `replay` of the origin run, identical binding, outcome `invariant_violated` with a violated verdict for the same invariant, backed by an observed, non-`asserted` observation. `confirmed` cannot be built or parsed without that `validation` block. `rejected` requires a disposition from `false_positive`, `not_reproducible`, `duplicate`, `out_of_scope`. Both are terminal; each transition seals a revision whose `supersedes` is the previous `finding_id`. A model-authored (`asserted`) finding may be `candidate` or `rejected`, and is `confirmed` only through validation.

Redaction: authoritative strings (and every string inside the reused
`ExecutionJoin`, `PolicyDecisionRef`, `ApprovalRef`, `ArtifactRef`) matching
`redaction.contains_credential` are refused. Model-authored text (`summary`,
`disposition_note`) goes through `redact_text`: a credential shape becomes
`[redacted]`, and text is whitespace-normalised and truncated (600 / 300 chars).
Sensitive inputs are `fixture_ref` content hashes, never inline.

## Reuse map

| Concern | Record reused, not copied |
|---|---|
| Execution identity (#196, ADR 011) | `execution_evidence.ExecutionJoin`; `runtime_execution_id` (`ex-`) required |
| Policy decisions (#197, ADR 014) | `execution_evidence.PolicyDecisionRef` |
| Approvals (#198) | `execution_evidence.ApprovalRef` (`aar_` + `intent_hash`) |
| Evidence (#199, ADR 018) | `ev-` ids, `ArtifactRef`, `AUTHORITIES` |
| Release pins (ADR 007) | `execution_evidence.VersionPins` |
| Hashing | `context_snapshot.hash_bytes` / `canonical_json` |

## Field authority

Classes: **authoritative** (authored by a reviewed change or stamped by the
orchestrator, trusted), **derived** (computed from other fields, never
accepted on trust), **model-authored** (untrusted, bounded, redacted).

| Record | Field | Class |
|---|---|---|
| all | `api_version`, `kind` | authoritative |
| all | `content_hash`, `scenario_id` / `allowlist_id` / `run_id` / `result_id` / `finding_id` | derived |
| Scenario | `name`, `version`, `description`, `target`, `setup`, `inputs`, `invariants`, `budgets`, `execution_mode`, `mutation_mode`, `permitted_action_kinds`, `permitted_tools`, `followup_templates` | authoritative |
| Target | `kind`, `ref`, `environment`, `allowlist_entry` (`allowlist_id`, `entry_id`, `content_hash`) | authoritative |
| Allowlist | `name`, `version`, `entries` (`entry_id`, `target_kind`, `ref`, `environments`) | authoritative |
| Budgets | `max_attempts`, `max_tool_calls`, `max_wall_clock_seconds`, `max_followups` | authoritative |
| Setup / input / invariant / template | `step_id`, `kind`, `fixture_ref`; `name`, `value`, `content_hash`; `invariant_id`, `statement`, `check_kind`; `template_id`, `parameters` (`kind`, `values`, `minimum`, `maximum`) | authoritative |
| Run | `binding`, `execution`, `mutation_mode`, `granted_budgets`, `attempt`, `started_at`, `ended_at`, `policy_decisions`, `approvals`, `relation`, `parent_run_id` | authoritative (stamped by the harness from governed records) |
| Run | `observations` (`observation_id`, `invariant_ids`, `status`, `evidence_id`, `artifact`, `authority`) | authoritative for `status` / refs; `authority` states how it was obtained |
| Run | `followup` (`template_id`, `parameters`) | model-authored, valid only after `validate_followup` |
| Binding | `scenario`, `runtime`, `target` (`kind`, `ref`, `revision`) | authoritative |
| Result | `run_id`, `binding`, `outcome`, `invariant_verdicts` (`invariant_id`, `verdict`, `observation_ids`), `consumed`, `evidence_ids` | derived from the run's observations, checked by `check_result_against_run` |
| Finding | `binding`, `origin_run_id`, `origin_result_id`, `invariant_id`, `state`, `evidence_ids`, `supersedes` | authoritative |
| Finding | `reported_by` (`authority`, `producer`), `summary`, `disposition_note` | model-authored (`summary`, `disposition_note`), producer-stamped (`reported_by`) |
| Finding | `validation` (`replay_run_id`, `replay_result_id`, `authority`), `disposition` | derived by `transition_finding` / triager-chosen from a closed set |

## Field names that contain an authority word

`allowlist_entry`, `allowlist_id`, `permitted_action_kinds`, `permitted_tools`
and `granted_budgets` name scope or budget data. They decide nothing. The tests
pin this exact set; any other field containing `allow`, `deny`, `permit`,
`grant` or `authorized` fails the field-name scan.

## Open questions

- Production mutation: v1alpha1 forbids it, stricter than "disabled by default". A later api version may add an opt-in mode gated on a durable approval.
- The allowlist's home (a gitops file reviewed by PR, or an mctl-api record) is undecided; this ADR defines only its shape and the pure check.
- The hard caps and the target-kind vocabulary are a first cut and must be confirmed before the harness depends on them. A cap change is an api-version bump.

## Follow-ups

The probe harness (consumes these contracts unchanged); Tier B persistence in
mctl-api; the allowlist home; an approval-gated production mode in a future api
version. Any CWFT, cron or values change belongs in mctl-gitops, not here.
