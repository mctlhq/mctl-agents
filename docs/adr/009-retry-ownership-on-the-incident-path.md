# ADR 009 — Retry ownership on the incident path

> **Status:** accepted
> **Date:** 2026-09-04
> **Context:** `mctlhq/mctl-agents#149`
> **Supersedes:** nothing — this records a split that already exists in code
> and was, until now, written down nowhere

## Context

A single incident tick crosses three systems that can each retry, and until
this ADR no document said which one owns a given failure:

1. **mctl-api / Argo** — `INCIDENTS_OPERATION` (`mctl-agents-incidents`) is
   submitted through `POST /api/v1/operations/{op}/execute`, and the
   ClusterWorkflowTemplate behind it has retries of its own, including the
   fallback to the second OAuth account when the first is out of quota.
2. **Temporal** — `submit_and_wait` carries
   `SDK_STEP_RETRY_POLICY = RetryPolicy(maximum_attempts=3)` with a two-hour
   `start_to_close` and a two-minute `heartbeat_timeout`
   (`orchestrator/temporal/workflows/incidents.py`).
3. **The schedule** — `incidents-mctl-agents-schedule` fires every 30 minutes
   with `overlap=SKIP`.

Three retrying layers with no stated boundary is how the same agent run gets
executed twice: the obvious reading of "the activity retries three times" is
"the responder runs three times", which is exactly what must never happen —
an SDK run costs money and writes proposals.

## Decision

Each layer owns exactly one failure class, and none of them owns another's.

**Argo owns retrying the work.** Anything that goes wrong *inside* a
responder run — a flaky tool call, an exhausted OAuth account, a step that
must run again — is retried by the CWFT, in the pod, where the checkout and
the commit step are. Temporal never re-runs a responder.

**Temporal owns resuming the wait.** `submit_and_wait`'s three attempts exist
so that a *worker* crash does not orphan a live Argo run. A retried attempt
does not re-submit: the first thing the activity does after a successful
submission is `activity.heartbeat(workflow_name)`, and a retry reads that
back from `activity.info().heartbeat_details` and goes straight to polling.
The retry budget therefore buys re-attachment, not re-execution.

The one case where the workflow name cannot be recovered — mctl-api accepted
the submission but its response did not parse — heartbeats the sentinel
`<submitted, workflow name unparseable>` and then **fails loudly on the next
attempt** rather than submitting again. A duplicated SDK run is worse than a
tick that needs a human to look at Argo.

**The schedule owns the next opportunity.** A responder run that genuinely
failed is not retried at all: `IncidentLoopWorkflow` *returns* a `Failed`
phase instead of raising, `record_execution` writes it to the audit trail,
and the next scheduled tick 30 minutes later picks the incident up again
because it is still unresolved. `overlap=SKIP` keeps a slow tick from being
joined by the next one.

Consequently a failed responder run is visible in exactly two places — the
`ExecutionRecord` and the Argo run — and in neither of them does it look like
three runs.

## Consequences

- **Raising `maximum_attempts` does not make the responder more reliable.**
  It only widens the window in which a crashed worker can re-attach to a run
  that is still going. Reliability of the run itself lives in the CWFT.
- **The heartbeat is load-bearing, not telemetry.** Dropping the
  `activity.heartbeat(workflow_name)` immediately after submission, or
  shortening `heartbeat_timeout` below the poll interval, converts every
  retry into a duplicate submission. This is why `submit_and_wait`
  heartbeats before *every* poll and not only on change.
- **`start_to_close` must outlive the pod it waits on.** If the activity
  gives up first it abandons a running pod — and, for the templates that
  take `mctl-gitops-main-writes`, a held mutex — and then retries on top of
  it. `orchestrator/validate_manifest.py` now checks this relation against
  the real templates (`_DERIVED_STEP_TIMEOUTS`), so it cannot drift silently.
- **A `Failed` phase must keep being returned, not raised.** Raising would
  hand the failure back to Temporal's retry policy, which would re-submit —
  taking ownership of a class that belongs to Argo and the schedule.

## Alternatives rejected

**Let Temporal retry the whole responder run** (raise on `Failed`, let
`maximum_attempts` re-submit). Simpler to read, and wrong: it duplicates paid
SDK work on failures that are usually deterministic, and it races the
schedule, which is already the retry mechanism at that granularity.

**Set `maximum_attempts=1`** so no layer can be confused about re-execution.
That removes the crash-resume the policy exists for: a worker restart during
a two-hour poll would orphan the Argo run and lose the audit record.

Related: ADR 006 (dev-loop stages), ADR 008 (queue split — why the poll holds
a slot for hours), `mctlhq/mctl-agents#179` (why the responder runs in Argo
at all).
