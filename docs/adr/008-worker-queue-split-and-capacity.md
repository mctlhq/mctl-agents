# ADR 008 — Control/execution queue split and capacity limits

> **Status:** accepted
> **Date:** 2026-08-31
> **Supersedes:** nothing — splits the single `mctl-dev-loop` queue the
> phase-4 worker introduced; every workflow and activity keeps its name

## Context

`mctl-agents-worker` is one replica running everything on one task queue
(`orchestrator/temporal/constants.py:TASK_QUEUE = "mctl-dev-loop"`): five
workflow types and fourteen activities, from `find_proposal_slug` (one
GitHub GET) to `submit_and_wait` (polls an Argo workflow every 15 s for up
to two hours). No concurrency limit is configured, so both sit in the same
default slot pool.

That was right for the pilot and is now the wrong shape, for a reason that
is arithmetic rather than aesthetic. Phase 6 gave `DevLoopWorkflow` a
**14-day** merge watch, and the production worker already reports 9–11
concurrent DevLoops. Every one of them can hold a `submit_and_wait` slot
for hours at a time. The Temporal Python SDK's default
`max_concurrent_activities` is 100 and there is exactly one pool: once
long pollers fill it, `find_proposal_slug`, `get_pr_state`,
`resolve_agent_release`, `record_execution`, reconcile and the issue
poller all queue behind them. Approval itself survives — a signal is a
workflow task, not an activity — but everything the approval then needs in
order to proceed does not.

So the failure mode is not "the worker is slow". It is: **a burst of agent
executions silently stops reconciliation and intake**, and nothing in the
configuration says how many is too many. #152.

Two properties make the split cheap, and both were verified rather than
assumed:

- **No agent runs in this process.** Every LLM/clone workload is an Argo
  submit. As of #149 the worker's import graph does not even contain the
  agent SDK, and `tests/test_worker_isolation.py` keeps it that way. So
  "execution" here means *waiting on* an execution, which is cheap per
  slot and long per slot — the opposite of what one pool serves well.
- **Schedules are already replica-safe.** `setup_schedules()` creates each
  schedule only if absent and otherwise updates the spec in place
  (observed in production: "already exists; spec is current"). Two
  replicas do not produce two schedules, and Temporal, not the worker,
  decides when a schedule fires — so a second replica cannot double-fire
  one. This is the property #152's third acceptance criterion asks for,
  and it holds today; the rollout below must not regress it.

## Decision

**D1 — Two queues, split by holding time, not by importance.**
`mctl-dev-loop` keeps workflows and every short activity. A new
`mctl-dev-loop-exec` takes `submit_and_wait` alone. The dividing line is
"can this occupy a slot for hours", because that is what starves a pool.
Naming it after importance ("control" vs "execution") describes the
consequence, not the criterion — a short activity in the critical path and
a short activity in reconcile belong together.

**D2 — One image, one deployment per role, selected by `--role`.**
`python -m orchestrator.temporal.worker --role control|execution|all`.
`all` stays the default so the current single deployment keeps working
unchanged, and so a rollback is a values edit rather than a code revert.

**D3 — Explicit slot limits per role, in configuration.**
The point is that capacity is something configuration states and metrics
can be read against, so exhaustion becomes a bounded backlog on one queue
instead of an invisible global stall. The numbers themselves are starting
values to be moved by D5, not derived constants.

**Neither control ceiling is lowered before step 3.** Between step 2 and
step 3 the control worker still carries every long Argo poll *and* all
five workflow types, so any ceiling below current behaviour in that
window reintroduces the starvation this ADR removes — for the whole soak,
with 9–11 concurrent loops in production. So `max_concurrent_activities`
is set to the 100 it replaces, and `max_concurrent_workflow_tasks` is
left unset: with no value the SDK does not fall back to 100, it builds a
**500**-thread pool, so any number there is a far larger step down than
it looks. Step 3 picks both values, in the same change that takes the
workload away and with D5's numbers in hand. Execution starts at 40 —
it is a new queue with no existing load to protect.

**D4 — The routing flip is guarded by `workflow.patched("exec-queue")`.**
`execute_activity(task_queue=...)` changes the scheduled command, and
in-flight histories replay command-for-command. With 14-day watches
running right now, an unguarded change would wedge every active loop —
the same failure the `slug-scoped-implement` and `atomic-approve` markers
exist to prevent.

**D5 — Metrics before tuning.** Temporal's SDK already exports
`temporal_worker_task_slots_available`, per-queue schedule-to-start
latency, and activity failure counts. Alert on sustained
schedule-to-start on the control queue — that is the starvation signal in
one number, and it is the thing that was previously unobservable.

**D6 — No HPA in this slice.** Replica count stays fixed per role. Both
roles are I/O-bound waiters, so CPU-based autoscaling would measure the
wrong thing entirely; the right signal is slot availability, which needs
a custom metrics adapter that does not exist yet. Fixed replicas with a
visible backlog is honest; an HPA on the wrong metric is not.

## Rollout order (fail-closed)

The order matters and it is the reverse of the intuitive one. A worker
polling the new queue must exist **before** any workflow routes work to
it — otherwise activities are scheduled onto a queue nobody polls and sit
there until timeout.

1. **mctl-agents:** `--role` flag, queue constants, slot limits. Routing
   unchanged; `all` behaves exactly as today. Safe to release alone.
2. **mctl-gitops:** second deployment `mctl-agents-worker-exec` with
   `--role execution`, and `--role control` on the existing one. Both
   poll; nothing yet schedules to the exec queue.
3. **mctl-agents:** flip `submit_and_wait` onto the exec queue behind
   `workflow.patched("exec-queue")`, and only now tighten the control
   limit (D3) — the same change removes the workload it would otherwise
   be squeezing.
4. Soak one full dev-loop, then tune D3 from D5's numbers.

Steps 1 and 2 are individually reversible. Step 3 is one-way for
histories that record the patch, which is why it is last and alone.

## Consequences

- Two deployments to keep in sync on image tag. The release pipeline bumps
  by `values_path`, which names one file — so step 2 must add the second
  path, or the exec worker silently pins to whatever tag it launched with.
  This is the same class of drift that left the shepherd on 1.25.0 for
  three weeks (#238), and it is the most likely way this ADR goes wrong.
- Memory doubles at idle (two 256Mi ceilings instead of one). At current
  scale that is noise against the Argo pods.
- `--role all` remains supported indefinitely, for local development and
  as the rollback target. It is not deprecated.

## Non-goals

HPA and per-agent quotas/cost budgets (D6 and #152's "per-agent quotas"
bullet — both need D5's metrics first); splitting reconcile onto a third
queue; any change to what runs in Argo.
