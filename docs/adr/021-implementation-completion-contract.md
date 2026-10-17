# ADR 021 — `ImplementationCompletion` contract (implementer-phase completion handoff)

> **Status:** proposed
> **Date:** 2026-10-17
> **Issue:** mctlhq/mctl-agents#428
> **Implements:** `orchestrator/implementation_completion.py` (schema),
> `orchestrator/run_implementer.py` (emission, projection, `--completion-out`)

## Scope

This is an **implementer-phase completion contract — not an independent
review verdict, not approval, not another DevLoop.** It is a typed, sealed,
versioned record of how one implementer attempt step ended. It describes the
existing implement / review-remediation phase; it is not a new lifecycle
owner, a nested loop, or a new retry / timeout / admission mechanism. The
ADR-010 claim, #395, #418, #423 and ADR 011 semantics are reused as they are.

Before this record, an attempt ended through four unversioned channels: the
`.status.yaml` status (`implemented`, `needs-triage`, `rate_limited` ...), the
exit codes 0 / 1 / 42-54, the optional `--refusal-out` JSON, and the
agent-authored `.implementer-refusal.json`. None of them says "attempt X, step
S of Argo run R, under claim Y, against proposal content D, produced head H",
so "a PR exists" stood in for "implementation complete".

## What `complete` means in v1alpha1

**`complete` means: the attempt pushed a head that passed the orchestrator
gates.** The gates are the four `required` checks, and only these:
`commits_present`, `chart_major_guard`, `push_succeeded`,
`claim_valid_at_push`.

It does **not** mean "the approved scope is implemented", and it is not a
review verdict: nothing compares the pushed head with the approved `tasks.md`.
The stop-reason code is therefore `gates_passed`, never `scope_done`.

**Follow-up F1 — task-coverage verification.** A future `task_coverage` check,
`required` in a later api version, that compares the pushed head with the
approved `tasks.md`. Until it exists, no consumer may read `complete` as
"done".

## The record

`api_version: completion.mctl.ai/v1alpha1`, `kind: ImplementationCompletion`: a
frozen dataclass with a canonical JSON form, a `sha256:` `content_hash`, and a
derived `completion_id` (`ic-` + 16 hex characters). `from_dict` rejects
unknown keys and unsupported api versions.

| Block | Content |
| --- | --- |
| `subject` | service, slug, optional `work_item_id` |
| `attempt` | `attempt_id`, `run_id` (the Argo workflow name, `WORKFLOW_NAME`), `step` (`primary` / `fallback`, from `IS_FALLBACK`), `phase` (`implement` / `review-remediation`), optional `ordinal`, `execution_id` (`we_`), `context_id` (`ex-`), `claim_id`, `owner_epoch` |
| `source` | repository, base ref and SHA, branch, head SHA or null |
| `proposal` | `proposal_digest` (`proposal_state.proposal_content_digest`, the framing `approval.content_digest` uses), optional `review_bundle_digest` |
| `outcome`, `stop_reason` | closed sets, below |
| `verification` | at most 32 of `{check_id, result, classification, required, evidence_ref?}`; no stdout / stderr field |
| `remaining_work` | at most 32 of `{item_id, summary}` |
| `evidence_refs` | at most 32 of `{evidence_id, kind}` only (ADR 018) |

`run_id` and `step` are part of the content hash, so the primary and fallback
records of one Argo run always have different `completion_id`s, whether or not
their `attempt_id`s coincide (`WORKFLOW_UID` unset gives each step its own).
`created_at` is excluded from the hash, so re-sealing an identical record
yields the same id.

Invariants, enforced at construction:

- `complete` requires every `required` check recorded and at `pass`, empty
  `remaining_work`, and a non-null `source.head_sha`;
- `incomplete` requires non-empty `remaining_work`;
- a stop-reason code belongs to exactly one outcome;
- `required` is a property of the contract version: exactly the four gates are
  required, and ledger-observed agent commands are recorded non-required;
- every string leaf is bounded; `stop_reason.detail` and
  `remaining_work[].summary` are redacted through `orchestrator/redaction.py`
  before sealing (a credential-shaped value is dropped, never partially
  masked).

### Outcome mapping

One pure function, `run_implementer.completion_from_result`, maps every exit
code and every `ImplementResult` arm to exactly one pair
(`tests/test_run_implementer_completion.py` fails if an `EXIT_*` constant is
added without an entry):

| Today's signal | outcome | stop_reason.code |
| --- | --- | --- |
| `implemented` write / `EXIT_OK` with a new head | `complete` | `gates_passed` |
| refusal marker with the optional `remaining` list, on a successful push | `incomplete` | `known_work_remains` |
| 47 `DELIBERATE_NO_OP` | `bounded_refusal` | `deliberate_no_op` |
| 50 `CI_EVIDENCE_INSUFFICIENT` | `bounded_refusal` | `evidence_insufficient` |
| 53 `POLICY_REFUSED` | `bounded_refusal` | `policy_refused` |
| 51 `VERIFICATION_BUDGET_EXHAUSTED` / budget hand-back | `budget_exhausted` | `verification_budget` |
| 44 `OPERATION_TIMEOUT` | `budget_exhausted` | `operation_timeout` |
| 52 `RATE_LIMITED` / `_mark_rate_limited` | `budget_exhausted` | `rate_limited` |
| 42 `NO_FOLLOWUP_COMMITS` / no commits | `deterministic_failure` | `no_commits` |
| 43 `BRANCH_MISSING_ON_ORIGIN` | `deterministic_failure` | `branch_missing` |
| `needs-triage` policy arms (chart guard, stale source) | `deterministic_failure` | `policy_gate` / `stale_source` |
| 46 `ORPHANED_SUBAGENT` | `harness_failure` | `orphaned_subagent` |
| 48 `FENCED` | `harness_failure` | `fenced` |
| 49 `CLAIM_REFUSED` | `harness_failure` | `claim_refused` |
| 54 `POLICY_UNDECIDED` | `harness_failure` | `policy_undecided` |
| 1 generic / unexpected exception | `harness_failure` | `unexpected_error` |

`incomplete` is reserved and intentionally unreachable in v1alpha1. The
classifier maps the new optional, bounded `remaining: [{item_id, summary}]`
list in `.implementer-refusal.json` (read after a successful push) to
`incomplete`, but the agent prompt forbids writing the marker next to a commit
and does not mention `remaining`, so there is no agent-facing channel yet. A
marker without `remaining` behaves exactly as before.

Exits before the attempt starts (approval and approval-digest blocks, GitHub
preflight, admission, `ensure_auth_for_sdk` failure, a refused acquire, the
token-2-unset shell exit, `EXIT_BLOCKED_ONLY`) emit **no** completion.

## Emission

Exactly one record per attempt step, built after the attempt ends, around the
unchanged attempt body (`_implement_attempt`, `_review_feedback_attempt`).
Emission logs and swallows its own errors and never changes an exit code, a
`--refusal-out` payload or a `.status.yaml` status transition.

- **`--completion-out PATH`** writes the full sealed record as JSON (best
  effort, bounded). The exit code remains the fallback signal. Nothing
  consumes the file yet.
- **`.status.yaml` projection** (implement mode, on the arms that write
  status). A bounded `completion:` block holds the authoritative summary, an
  optional summary of the other step, and `primary_observed`: no verification
  details, no telemetry. Review-feedback mode does not write status (the
  shepherd owns it), so it only hands the record back on the result and to
  `--completion-out`.
- **Stale claim.** If the attempt's claim, still held at emission, fails
  `_check_claim_or_raise`, no projection is written and the record is
  restated as `harness_failure` with `fenced` or `claim_refused`. A claim
  already released is not re-checked.

```yaml
completion:
  api_version: completion.mctl.ai/v1alpha1
  run_id: mctl-agents-implement-abc12
  authoritative:            # the record that speaks for this Argo run
    completion_id: ic-...
    content_hash: sha256:...
    attempt_id: ...
    step: primary
    outcome: deterministic_failure
    stop_reason: no_commits
    head_sha: null
    verification: {required_passed: 1, required_total: 4, optional_total: 1}
  other:                    # optional: the non-authoritative step's summary
    relation: fallback      # fallback | superseded
    completion_id: ic-...
    step: fallback
    outcome: harness_failure
    stop_reason: rate_limited
  primary_observed: true
```

### Replay key

Keyed on `(run_id, step, attempt_id)`. The same key and the same
`completion_id` is a no-op; the same key and a different `completion_id` is
refused and logged; a write whose attempt no longer owns the status
(`.status.yaml` names a different `attempt.id`) is refused. A pod restart or
Argo retry inside one step therefore cannot flip `budget_exhausted` /
`harness_failure` into `complete`.

## Primary / fallback authority

The CWFT runs `implement` (primary), then `implement-fallback` (second
account, only when the first did not succeed), then `commit-and-push`. Both
steps upload the same artifact key and the fallback's gitops clone cannot see
what the primary wrote.

**Within one Argo run the primary record is authoritative unless the fallback
is `complete`.** A failing fallback (typically a harness failure on the second
account) never replaces the primary's outcome; a succeeding fallback does, and
the primary is kept as a `superseded` summary. This mirrors `assert-produced`,
which counts the run as produced if either step succeeded.
`select_authoritative(primary, fallback)` is a pure function:

| primary handoff | fallback outcome | authoritative | other |
| --- | --- | --- | --- |
| present, any outcome | `complete` | fallback | primary as `superseded` |
| present, any outcome | not `complete` | primary | fallback as `fallback` |
| absent (not readable) | any | fallback, `primary_observed: false` | none |
| fallback never reached the claim | no record | primary (artifact untouched) | none |

The primary always writes `authoritative = own record`.

The fallback reads the primary's handoff from the optional directory
`/tmp/prior-state` (`_load_prior_handoff`): a bounded read of `.status.yaml`,
extracting only `completion`, validated, and accepted only when its `run_id`
equals the fallback's own `WORKFLOW_NAME` and its authoritative `step` is
`primary`. Anything else is treated as absent. Until mctl-gitops#1947 mounts
that input, the directory does not exist and the fallback projects with
`primary_observed: false`; this interim state is accepted by the owner and is
visible, not silent.

The status *transition* the fallback writes (for example `rate_limited` over
the primary's `needs-triage`) is unchanged existing behaviour. Only the
`completion:` block follows the authority rule, so `completion.authoritative`
(the attempt's result) and `status` (the lifecycle state) may legitimately
differ after a failed fallback.

## Carrier boundary

- **git** (`.status.yaml`): the durable result projection, bounded.
- **Temporal / Argo**: the runtime; nothing here changes the node graph.
- **mctl-api**: the read model, if and when one is added. No storage columns
  are part of this ADR.
- Durable evidence storage stays ADR 018 Tier B.

## Non-authorities

The record cannot express, and `from_dict` rejects as unknown keys: approval,
a review verdict, review-clean, merge, release, ownership transfer, claim
extension, or budget extension. The implementer agent does not author the
record; the orchestrator seals it from what it observed. The shepherd's merge
decision never reads `outcome`.

## Consumers (follow-up)

Not part of this change; each extends this ADR with its own section:

- shepherd shadow consumption (`--completion-out`, `completion-disagreement`
  logging, `SHEPHERD_TRUST_COMPLETION` default off):
  mctlhq/mctl-agents#679;
- Temporal observe-only (`ExecutionRecord` optional fields,
  `read_proposal_completion`, `workflow.patched("implement-completion")`):
  mctlhq/mctl-agents#680;
- the optional `/tmp/prior-state` input artifact on `run-implementer`, at the
  existing key `{{workflow.name}}/agents-state.tgz`:
  mctlhq/mctl-gitops#1947 (may merge before or after this change; the
  implementer behaves identically either way).

## Open questions

- `incomplete` is reserved and unreachable in v1alpha1 (no agent-facing
  channel for `remaining`). Decide when and how to expose one.
- The record carries `proposal_content_digest`, which the implementer already
  checks against `approval.content_digest` before starting (#658). Whether to
  also carry the recorded `approval.content_digest` is deferred to a later
  version.
- Whether the implement CWFT should set `WORKFLOW_UID` is a separate decision.

## Rollback

Additive. Reverting removes the schema module, `--completion-out`, the
projection and the authority logic. A stray `completion:` key left in
`.status.yaml` is ignored by every existing reader (`proposal_state.load_status`
and `gitops_state._parse_status_yaml` read named keys), so no data cleanup is
needed.
