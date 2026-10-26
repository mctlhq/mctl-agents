# ADR 024 — SecurityFinding remediation and verification runner

> **Status:** proposed
> **Date:** 2026-10-26
> **Issue:** mctlhq/mctl-agents#809 (supersedes #778; epic: mctlhq/.github#209; store: mctl-api#774)
> **Implements:** `orchestrator/security_findings/`, `orchestrator/run_security_verifier.py`,
> `orchestrator/temporal/activities/security_findings.py`
> **Contract:** mctl-api `4.124.0`, `docs/security-findings.md` and `internal/openapi/openapi.yaml`,
> captured in `tests/fixtures/security_finding_v1/`

## Decision

mctl-api decides every SecurityFinding state change and audits every write. mctl-agents is a client with
three writes: start a `RemediationAttempt`, bind the landed revision (`fix`), and record a `verification`.
It never decides a state locally, never posts observations, and never triages or dismisses.

**Merge is not resolution.** A merged PR binds the merge commit and moves the occurrence to
`awaiting_verification`. Only a verification bound to that exact revision (or artifact digest), whose derived
strength meets mctl-api's minimum, resolves it. The DevLoop hook never records a verification.

## Boundary

| Concern | Owner |
|---|---|
| Transitions, strength derivation, dedup by `observation_hash`, reopen, stale-scan filter | mctl-api (#774) |
| Provider ingest and configuration | mctl-gitops#2103 (providers only) |
| Replay semantics, `AssuranceFinding`/`AssuranceResult` | Runtime Assurance (ADR 022, 023); consumed, not re-implemented |
| Start, bind, verification strategy and its budget | this package |
| Scheduling the sweep | a CronWorkflow in mctl-gitops (follow-up PR) |

## Trust rules

- Provider text is inert. `untrusted.*` is dropped at parse time (`contract.py`); the parsed types have no
  attribute for it. Strategy choice reads structure only, and the regression check comes from the
  PR-reviewed `config/security_regression_tests.json`, never from provider evidence.
- Closed vocabularies come from the 4.124.0 fixtures. A value outside them parses as `unknown`, and a code
  outside them makes an answer `unknown`, never `ok`.
- A 503, a transport error or a malformed body is `unknown`. It is never "no occurrence" and never "no
  active attempt".
- No strength is sent. `provider_claim` and `manual` are never recorded by the agent.
- Local binding guard: an outcome whose revision or digest differs from the attempt's landed one is dropped
  (`local_not_bound`). A server 422 `verification_not_bound` is final.

## Transient results

`unavailable`, `error` and `timeout` are recorded as such and keep the occurrence `awaiting_verification`.
They are not `unverifiable`. `unverifiable` is recorded only once the budget is exhausted: more than
`SECURITY_VERIFY_MAX_TRANSIENT` (3) transient records for the attempt, or `SECURITY_VERIFY_DEADLINE_H` (72)
hours since `landed_at`, with no `passed`/`failed`. Both counts are read back from the occurrence's own
`verifications[]`. A scanner strategy that has found nothing yet is pending and records nothing.

## Credentials

Every call uses `run_token.mctl_api_bearer()`. In the Temporal workers that is `MCTL_TOKEN`. mctl-api grants
`security:remediate` only to its static service principal (or a run token minted with it, confined to its own
work item), so the writes succeed only if `MCTL_TOKEN` is that principal. A 403 is reported as
`refused(forbidden)` with the route and bearer kind, stops all further writes in the run, and makes the sweep
exit non-zero. The sweep refuses `MCTL_RUN_TOKEN_MODE=enforce` before any call.

## Open questions

- Whether `mctl-token` equals `mctl-api-token` (owner precondition; the loud 403 makes the gap visible).
- The transient budget defaults (3 records or 72 h) need confirming.
- No "N independent observations" threshold exists in #774; none is implemented here.
- There is no dedicated schema field naming a regression test; the reviewed mapping stands in.
- `GET /occurrences` has no `devloop_workflow_id` filter. The bind lists by state and matches the repository
  and workflow id client side.
- The assurance strategy has no default store for findings and replay results, so it is not applicable
  until a source is wired in.

## Rollback

Additive. Reverting removes the package, CLI and activity. Keep `workflow.patched("security-record-landed")`
for one release so in-flight histories replay. Kill switches without a deploy: do not schedule the sweep, or
set mctl-api `SECURITY_FINDINGS_DISABLED=true` (every route answers 503; the hook and sweep answer `unknown`).
