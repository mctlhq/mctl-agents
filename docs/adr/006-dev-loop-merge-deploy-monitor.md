# ADR 006 — Phase 6: dev-loop merge → deploy → monitor

> **Status:** accepted
> **Date:** 2026-08-29
> **Supersedes:** nothing yet — extends `DevLoopWorkflow` past implement;
> `cronworkflow-mctl-agents-shepherd.yaml` is narrowed (sweeper), not removed

## Context

`DevLoopWorkflow` ends at implement: it submits the implement CWFT, records
the result, and returns (`workflows/dev_loop.py`). Everything after — review
fix loops, merge, the release reaching the cluster, and whether the rollout
broke anything — happens outside the workflow, or not at all:

- **Review/merge** is the shepherd (Tier 3), a standalone Argo cron
  (`cronworkflow-mctl-agents-shepherd.yaml`, every 2 h 07:00–21:00) driving
  ALL actionable proposals. ADR-005's replacement table already names its
  destination: "review-loop stage inside DevLoopWorkflow".
- **Merge detection** is polling by two outsiders: the shepherd cron and
  `ReconcileWorkflow` (15 min, projects `implemented → merged`). Neither
  signals the DevLoopWorkflow; mctl-api has no GitHub webhook receiver.
- **Deploy** is fully automated but unobserved by the loop: release-please
  on the app repo dispatches mctl-gitops `release-deploy.yaml` (image bump
  commit as mctl-ci), ArgoCD auto-syncs within ~30 s.
- **Monitoring** does not exist per-loop. `IncidentLoopWorkflow` is global,
  its schedule is created paused (#179), and incidents carry no link to a
  release or PR.

Phase 6 closes the loop: one issue's workflow should end "merged, deployed,
healthy (or: here's what broke)". Tracker: #217.

## Decision

Extend `DevLoopWorkflow` with four stages, each behind its own
`workflow.patched(...)` marker (same replay-safety pattern as
`slug-scoped-implement` and `atomic-approve`). **Slice 1 is stage 6.1
only** — proven observable in Temporal history before any deploy-side stage
lands.

### 6.1 Review loop + merge detection (#213, #214; prereq #67)

After implement succeeds, the workflow drives its OWN proposal to merge:

- A bounded loop of `_run_cwft("mctl-agents-shepherd", {service, slug, ...})`
  ticks with `workflow.sleep(interval)` between them, replacing the global
  cron as the driver for proposals that HAVE a live DevLoop.
- Merge detection via a new `get_pr_state(repo, pr_number)` activity
  (GitHub API, existing `GITHUB_TOKEN_FILE` plumbing) polled in the same
  loop. **Polling, not a webhook**: no new ingress/HMAC surface in mctl-api
  for slice 1; a `pr_merged` signal endpoint (mirroring `SignalApprove`) is
  a later optimization that does not change the workflow's shape.
- **Prerequisite #67**: shepherd's `decide()` must read
  chatgpt-codex-connector[bot] findings before any in-loop automation of
  the merge gate — otherwise phase 6 automates a known reviewer blind spot.
- **Cron ownership**: the global shepherd cron becomes a **sweeper** for
  proposals with no DevLoop (pre-Temporal, `incident-*`,
  `SHEPHERD_SKIP_SERVICES`) — "kept as a migration path and safety net,
  not deleted" (`docs/agent-inventory.yaml`). It must skip slugs owned by a
  live DevLoop; the ownership marker (a `.status.yaml` field vs a Temporal
  visibility query) is decided in #213 — the `.status.yaml` field is the
  default candidate because the sweeper already reads those files and has
  no Temporal client.
- **Lifetime**: the workflow now lives until merge (days). `SDK_STEP_TIMEOUT`
  stays per-step; the loop itself is bounded by tick count/deadline, and the
  workflow-ID reuse policy in `start.py` must be reviewed so a re-filed
  issue does not collide with a still-running loop.
- **Cost**: each shepherd tick provisions a Hetzner volume — tick cadence is
  hours, not minutes, and each tick must be justified by a PR-state change
  (poll `get_pr_state` first, submit the shepherd CWFT only when there is
  something for it to do).

### 6.2 Release observation (#215)

After merge: watch the release land, using only existing read surfaces —
poll `GET /api/v1/status/{team}/{app}` until the new revision appears.
Needs a `repo → (team_name, component_name)` mapping activity; today that
mapping exists only inline in each repo's `release-please.yml` dispatch
args. No new deploy machinery: release-please + `release-deploy.yaml` +
ArgoCD auto-sync already do the work; the loop only observes.

### 6.3 Post-deploy verify (#215)

An activity that waits, with a bounded timeout, until the app reports
Synced/Healthy with the new image tag (same status endpoint). Timeout or
Degraded surfaces as a failed stage in `DevLoopResult` — **no
auto-rollback**; `wft-rollback-service` stays human/MCP-invoked.

### 6.4 Bounded incident watch (#216)

A scoped query — `GET /api/v1/incidents` filtered by the deployed service
and a bounded window from the deploy timestamp — NOT the global
IncidentLoopWorkflow (paused, #179, and unable to answer "did MY rollout
break something"). Observed incidents land in `DevLoopResult`; escalation
uses the existing notify path. Correlation is service+window only; causal
attribution is #195/#196 (execution tracing/context) territory.

## Governance interaction

The in-loop merge decision (6.1) is #198's first candidate approval
checkpoint (`github.merge`). The stage must be shaped so a
`REQUIRE_APPROVAL` gate — a durable wait, approval bound to exactly this
PR's head SHA, approver identity recorded — can slot in front of the merge
call without restructuring the loop. The atomic approve stage (#150) is the
existing template for that shape.

## Non-goals

- GitHub webhook receiver in mctl-api (polling first; signal later).
- Un-pausing `IncidentLoopWorkflow` (#179 decides that on its own merits).
- Auto-rollback on failed verify.
- Control/execution task-queue split (#152) — orthogonal capacity work.
- Deleting the shepherd cron (it narrows to a sweeper).

## Implementation map

```
orchestrator/temporal/activities/pr_state.py      # get_pr_state (6.1)
orchestrator/temporal/activities/deploy_status.py # service status reads (6.2/6.3)
orchestrator/temporal/activities/incidents_query.py # scoped incident query (6.4)
orchestrator/temporal/workflows/dev_loop.py       # stages, each behind workflow.patched
orchestrator/temporal/worker.py                   # register new activities
```

gitops: `cronworkflow-mctl-agents-shepherd.yaml` gains the ownership-skip
(sweeper mode); no new CWFTs expected for 6.2–6.4 (read-only stages).

Issue order: #67 → #213/#214 (slice 1) → #215 → #216.
