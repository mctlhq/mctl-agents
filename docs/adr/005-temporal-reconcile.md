# ADR 005 — Reconcile loop on Temporal (replaces Argo `mctl-agents-reconcile`)

> **Status:** accepted
> **Date:** 2026-08-06
> **Supersedes:** Argo `cronworkflow-mctl-agents-reconcile.yaml` (now `suspend: true`)

## Context

The dev-loop orchestration moved from Argo CronWorkflows to Temporal in
`mctl-agents` 1.23.0. Five cron-based loops were left behind when Phase 4
shipped the first `DevLoopWorkflow` slice. This ADR lands the first of those
migrations: the **reconcile loop**.

Today reconcile lives inside `orchestrator/run_shepherd.py` as a
`--reconcile` mode and is triggered by the Argo CronWorkflow
`cronworkflow-mctl-agents-reconcile.yaml` (every 15 min). That cron is now
suspended (`suspend: true`, gitops PR #722) and will be deleted once the
Temporal version is proven.

The reconcile loop has a different safety profile from the other four cron
loops:

| Loop | Calls Claude SDK? | Replacement |
|---|---|---|
| issue-poll | no (poller only) | Temporal Schedule → `DevLoopWorkflow` |
| implement | yes | stage inside `DevLoopWorkflow` |
| shepherd | yes | review-loop stage inside `DevLoopWorkflow` |
| incidents | yes | `IncidentLoopWorkflow` |
| **reconcile** | **no** | **Temporal Schedule → `ReconcileWorkflow`** |

Reconcile is **read-only with respect to the SDK**: it performs GitHub reads
and projection onto proposal status. It is the natural first migration
because it carries zero quota-burn risk and exercises Temporal primitives
(schedule → workflow → activity → orphan detection).

## Decision

Replace the Argo `mctl-agents-reconcile` CronWorkflow with a **Temporal
Schedule** that starts one **`ReconcileWorkflow`** per tick. The workflow
replicates the existing read-only semantics:

1. Discover every non-terminal `.status.yaml` across
   `agents-state/<svc>/proposals/<slug>/`.
2. Group proposals by service, preserving on-disk order.
3. For each proposal in an actionable status: read its canonical PR state
   from GitHub and project that onto local status.
4. Detect **orphans** — proposals whose status says they should be in flight
   but which have no matching `DevLoopWorkflow` running in Temporal — and
   emit a structured signal (log + execution record) so the operator can
   adopt them.

The loop runs on a Temporal Schedule (cron `*/15 * * * *`, matching the
suspended Argo cadence) inside the existing `mctl-agents-worker` deployment
(gitops `services/admins/mctl-agents-worker/`) — no new Kubernetes workload.

## Semantics

### Status state machine (unchanged — `orchestrator/proposal_state.py`)

```
proposed → accepted → implemented → review-fixing → merged
                                                ↘ rejected
                                                ↘ review-stuck
                                                ↘ needs-triage
                                                ↘ error
```

`merged`, `rejected`, `review-stuck`, `needs-triage`, `error` are
**terminal** — reconcile never mutates a proposal already in one of these.
`accepted`, `in-progress`, `implemented`, `review-fixing` are
**actionable** — these are the only statuses reconcile inspects.

### Projection rules (read-only, GitHub-first)

For each actionable proposal, reconcile fetches the canonical
`feat/agents-<slug>` PR and projects:

| GitHub PR state | Local status | Projection |
|---|---|---|
| open / merging | anything actionable | leave (in flight) |
| open | missing from disk | create `.status.yaml` (adopted orphan) |
| merged | implemented / review-fixing | → `merged` |
| closed (not merged) | actionable | → `rejected` |
| gone (branch deleted, no PR) | actionable & age > N days | → `needs-triage` |

Projection **only ever narrows toward terminal** — it never re-opens a
terminal status and never moves a proposal backwards along the happy path.

### Orphan definition

A proposal is an **orphan** when ALL hold:

- `.status.yaml` exists with an actionable status (`accepted`, `in-progress`,
  `implemented`, `review-fixing`).
- Its canonical branch `feat/agents-<slug>` resolves to an open PR (so the
  work is real, not a pre-PR draft).
- **No** `DevLoopWorkflow` with Temporal workflow ID
  `dev-loop-{owner}-{repo}-{slug}` (or service/slug key) is currently running
  in namespace `mctl-agents`.

### Gitops commit

Gitops commits for `.status.yaml` updates remain delegated to the existing Argo CWFT
(`cwft-mctl-agents-reconcile.yaml`) under the shared `mctl-gitops-main-writes`
mutex. Temporal `ReconcileWorkflow` performs read-only projection and orphan detection.

## Implementation

New files in `mctl-agents`:

```
orchestrator/temporal/workflows/reconcile.py   # ReconcileWorkflow
orchestrator/temporal/activities/discovery.py  # discover + project (read-only)
orchestrator/temporal/activities/orphans.py    # orphan detection (Temporal list)
```

Changes:

```
orchestrator/temporal/worker.py                # register ReconcileWorkflow + schedule
```

The schedule is created in `worker.py` during worker startup.
