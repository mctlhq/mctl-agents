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
`python -m orchestrator.temporal.worker --role control|execution|implementation|all`
(`implementation` added by D7). `all` stays the default so the current
single deployment keeps working unchanged, and so a rollback is a values
edit rather than a code revert.

**D3 — Explicit slot limits per role, in configuration.**
The point is that capacity is something configuration states and metrics
can be read against, so exhaustion becomes a bounded backlog on one queue
instead of an invisible global stall. The numbers themselves are starting
values to be moved by D5, not derived constants.

**Neither control ceiling is lowered until the last step.** Until the
routing flip the control worker still carries every long Argo poll *and*
all five workflow types, so any ceiling below current behaviour in that
window reintroduces the starvation this ADR removes — for the whole soak,
with 9–11 concurrent loops in production. So `max_concurrent_activities`
is set to the 100 it replaces, and `max_concurrent_workflow_tasks` is
left unset: with no value the SDK does not fall back to 100, it builds a
**500**-thread pool, so any number there is a far larger step down than
it looks. Execution starts at 40 — it is a new queue with no existing
load to protect.

Tightening therefore comes **after** the flip and after the soak, not
with it. An earlier draft of this ADR put the tightening in the same step
as the flip while D5's exporter arrived in the step after, which asked
operators to pick numbers from data that did not exist yet (codex P2 on
#249). Two conditions gate the change and both are now upstream of it:
the workload has moved off the control queue, and the exporter is running
so the effect is visible.

**D4 — The routing flip is guarded by `workflow.patched("exec-queue")`.**

> **Amended 2026-09-02 (#251).** The reason originally given here was
> wrong, and the replay infrastructure built for this step is what
> disproved it. It said `execute_activity(task_queue=...)` "changes the
> scheduled command, and in-flight histories replay command-for-command",
> so an unguarded change "would wedge every active loop".
>
> It would not. `Replayer` compares the *shape* of the command stream —
> how many commands, of what kind, in what order. sdk-core's
> `handle_command_event` pops the next command off the queue and feeds it
> the event; it never compares the command's attributes to the event's,
> and the activity state machine has no `matches_event` at all. Measured
> on temporalio 1.31.0 against real recorded histories: an added, removed
> or reordered activity raises `NondeterminismError`; a changed
> `task_queue`, a changed timeout and an extra argument are all invisible.
> The table and the mutations are in `tests/test_workflow_replay.py`.
>
> The marker is still the right call, for reasons that survive:
> history records which executions used which queue, and a rollback of
> the flip is replayable rather than a second unguarded edit. But the
> flip was never the hazard this paragraph claimed, and two other places
> inherited the same false premise — `reconcile.py`'s unpatched branch
> (an accepted agy P1 about "payload mismatch on replay") and this ADR.
> Both corrected.
>
> The practical consequence for the rollout: **a green replay run is not
> evidence that the flip was guarded or that it routes.** Routing is
> verified from recorded history content, which is the only place a task
> queue is visible — see the `*.patched.json` fixtures and
> `test_todays_code_still_routes_and_still_guards`.

**Migration is by attrition, and that was got wrong too.** An earlier
version of this paragraph said an in-flight execution "replays its earlier
submits on the control queue and routes only its NEW ticks to exec". It
does not. `workflow_patch` memoizes per patch id
(`temporalio/worker/_workflow_instance.py`): an execution whose history
lacks the marker replays that call, gets False, memoizes False, and
returns False for the rest of its life — long after replay has finished.

So a dev loop that was already running when the flip deployed keeps
sending **every** Argo poll to the control queue until it ends, up to
`MERGE_WATCH_DEADLINE`. Only executions started after the deploy route to
exec. Verified end to end rather than read off the source, in
`tests/test_patch_memoization.py` — reasoning from source is exactly how
the wrong version got written.

Two operational consequences:

- A control queue still carrying long polls a week after the flip is
  attrition, not a broken flip. `temporal_worker_task_slots_used` on
  `mctl-dev-loop` decaying toward zero as old loops finish is the signal
  that it worked.
- The relief is therefore gradual. The alternative — dropping the marker
  and routing unconditionally — would move every in-flight execution
  immediately, and is genuinely available now that replay is known to
  ignore `task_queue` (agy proposed exactly that on #251). It is not taken
  because history would then contain no record of which queue an execution
  used, a rollback would be a second unguarded edit with the same gap, and
  a loop that switched queues mid-life would split its schedule-to-start
  across both — muddying the one measurement D5 exists to produce.

**D5 — Metrics before tuning, and the exporter is a prerequisite rather
than a given.** The SDK *can* produce
`temporal_worker_task_slots_available`, per-queue schedule-to-start
latency and activity failure counts, but it does not export them merely
because limits are configured: this worker calls `Client.connect` with
the default runtime, and the repository has no `TelemetryConfig` or
`PrometheusConfig` anywhere (checked — codex P2 on #249). Wiring an
exporter and scraping it therefore comes before the flip, and before any
number in D3 can be tuned against reality. The signal to alert on is
sustained schedule-to-start on the control queue: that is starvation in
one number, and it is what was previously unobservable.

*Resolved (#252).* `main()` now builds one `Runtime` with a
`PrometheusConfig` before the client and passes it to `Client.connect`;
the exporter binds `0.0.0.0:8080`, which is the port base-service already
declares as `http` and nothing was listening on. Verified against a live
worker rather than assumed — the names the rules and dashboards may use
are:

| metric | labels |
| --- | --- |
| `temporal_activity_schedule_to_start_latency_{bucket,count,sum}` | `namespace`, `service_name`, `task_queue` |
| `temporal_workflow_task_schedule_to_start_latency_{bucket,count,sum}` | same |
| `temporal_worker_task_slots_available` / `_used` | + `worker_type` (`ActivityWorker`, `WorkflowWorker`, `LocalActivityWorker`) |
| `temporal_num_pollers` | + `poller_type` |

`durations_as_seconds=True`, so buckets are `le="0.1"`, not `le="100"`.
Two facts worth writing down because they are not guessable: the roles
are **not** distinguishable by `service_name` (both report
`temporal-core-sdk`) — `task_queue` is what separates them; and
`counters_total_suffix` does nothing on temporalio 1.31.0, so no counter
carries a `_total` suffix regardless of the flag.

**D6 — No HPA in this slice.** Replica count stays fixed per role. Both
roles are I/O-bound waiters, so CPU-based autoscaling would measure the
wrong thing entirely; the right signal is slot availability, which needs
a custom metrics adapter that does not exist yet. Fixed replicas with a
visible backlog is honest; an HPA on the wrong metric is not.

**D7 — A third queue for admission, not for holding time (amended
2026-09-19, mctlhq/mctl-agents#395).** D1's criterion was "can this occupy a
slot for hours", and by that criterion the implementer submit belongs on
`mctl-dev-loop-exec` with every other long Argo poll — which is where step 4
put it. The 2026-09-19 incident showed the criterion is necessary but not
sufficient for one operation. Nine approvals in twenty minutes produced nine
`submit_and_wait` activities, all admitted at once by a 40-slot pool, all
submitted to Argo at once, and then serialised INSIDE Argo on the capacity-1
mutex `mctl-agents-proposal-claims`. Six of them died of their own workflow
deadline before reaching the head of that queue, and nothing in Temporal
knew: the DevLoops completed as `Completed`.

The queue was in the wrong layer. Temporal is the orchestration authority;
"may this run now" is its decision to make, before Argo sees anything.

So the split gains a queue whose criterion is **admission**:

```text
mctl-dev-loop             control / short activities
mctl-dev-loop-exec        every long Argo wait EXCEPT the implementer — stays at 40
mctl-dev-loop-implement   ONLY mctl-agents-implement, max_concurrent_activities = N
```

A `--role implementation` worker serves it; the activity is still
`submit_and_wait`. With no free slot the activity stays Scheduled in
Temporal — no Argo workflow, no Argo deadline — and the schedule-to-start
latency on this queue is, by construction, the queue age.

Three things D7 deliberately does not do, each for a reason recorded in
#395:

- **It does not lower exec from 40.** Investigate, reconcile and incidents
  are independent of implementer capacity; capping their pool to N would
  re-couple exactly the workloads D1 separated.
- **It does not add `schedule_to_start_timeout`** to the implement submit.
  `submit_and_wait` is scheduled with `start_to_close_timeout` only, so
  the queue wait neither consumes the execution budget nor is bounded by
  it. A schedule-to-start timeout would turn a capacity wait into a
  failure, which is the shape of the bug being fixed one layer earlier.
- **It does not remove the Argo mutex.** The admin-only direct
  `mctl_trigger_implementer` bypasses Temporal admission entirely; the
  mutex still guards that path until ADR-010's server-side claim is at
  `enforce` (mctlhq/mctl-api#337).

**N is a process limit, not a distributed semaphore.** Capacity is
`replicas × N`, so the implementation deployment runs one replica as an
architectural invariant of this phase, and mctl-gitops fails CI if that
number changes (mctlhq/mctl-gitops#1285). Worker slots are backpressure,
not a lease: a worker that dies after submitting leaves Argo running while
Temporal retries the activity on heartbeat timeout, and in that window the
slot is gone. The contract is therefore "under normal worker operation no
more than N implementation submissions are actively supervised, and queued
DevLoops submit no Argo work at all" — not "never more than N implementers
exist". The hard count across crashes belongs to ADR-010's claims.

N is read from `IMPLEMENTATION_MAX_CONCURRENT_ACTIVITIES` in the
environment (default 3, see the 2026-09-19 amendment below for the
mutex-bound default that now applies while `run-implementer` is still
guarded) rather than fixed in `constants.py`, because it is the one number
D5 expects an operator to move, and the values file that sets it is where
the queue-age alert that says it is wrong lives.

The rollout repeats steps 1–4 in the same fail-closed order: role and
queue first (mctl-agents#397, 1.47.0), the deployment polling an empty
queue second (mctl-gitops#1287), the routing flip behind
`workflow.patched("implement-queue")` third, attrition fourth. Only the
implement submit is routed; `incidents.py` and `reconcile.py` keep
`exec-queue`.

**What the flip carries with it, and why it is the same change.** Moving
the queue alone would have left the other half of the incident in place:
an implementer that never ran and one that ran and failed both ended the
loop as `Completed` with `implement.phase == "Failed"`, indistinguishable
from success in every list of running loops. So the same release, behind
a second marker `implement-outcome`, reads Argo's node graph for the
implement operation (`implement_outcome.py`) and classifies the result:

```text
pre_start     no run-implementer pod ever ran     → resubmit, ≤ MAX_PRESTART_REQUEUES,
                                                     no attempt counted, no human
execution     the pod ran and did not succeed     → workflow FAILS, ImplementationFailed
finalization  the pod succeeded, commit/assert    → workflow FAILS, ImplementationFinalizationFailed
              did not
success       the pod ran and committed           → the loop continues to merge detection
```

One honest limit of the rollout, measured rather than assumed: nothing in
CI proves that a loop already running when `implement-outcome` deploys
still completes on a failed implement. A history recorded before the
marker ENDS at that failure — completing there is the old behaviour — so
the only divergence today's code could produce sits in the final workflow
task, and the replayer does not compare it (the capability table in
`tests/test_workflow_replay.py` now records this, verified both ways). The
SDK half of the promise, that an execution which replayed a missing marker
keeps taking the unpatched branch for life, is covered by
`tests/test_patch_memoization.py` against a real server. The branch is
short-lived: once every loop started before the flip has passed
`MERGE_WATCH_DEADLINE`, the guard and this paragraph go together.

"Ran" is read from the marks a pod leaves on its node (`hostNodeName`, an
exit code, a Succeeded phase) — never from the node's `startedAt`, which
Argo stamps at node creation while the node may still be Pending on a
mutex. Unknown (no node graph) is `execution`, because resubmitting an
implementer that may have run is the duplicate-attempt failure the
heartbeat-resume design exists to prevent.

The attempt therefore begins when a pod is observed running, not at
approval, not when a worker slot admits the activity, not when Argo
accepts the workflow. `submit_and_wait` publishes that progression —
`admitted → submitted → running`, with timestamps — as its second
heartbeat detail. That heartbeat, read through Temporal's describe API,
plus the workflow's `implement_execution` query (queued_at, requeues,
outcome) is the runtime-state surface mctl-agents#389 projects from. Git
remains durable lifecycle state; nothing is pushed there mid-attempt.

> **Amended 2026-09-19 (mctlhq/mctl-agents#418).** "N and the mutex
> capacity have to move together" above was prose, and prose that nothing
> checked: N defaulted to 3 while the mutex admitted 1, so two of every
> three admitted implement submits still queued INSIDE Argo against
> `run-implementer`'s `activeDeadlineSeconds` — the exact 2026-09-19 shape,
> just smaller, reproduced by the admission queue this decision record adds.
>
> The binding is now code, not a comment. `orchestrator/temporal/constants.py`
> mirrors the CWFT's lock as `ARGO_IMPLEMENT_MUTEX_NAME`,
> `ARGO_IMPLEMENT_MUTEX_TEMPLATE` and `ARGO_IMPLEMENT_MUTEX_WIDTH` (the worker
> deployment has no gitops checkout to read them from live), and
> `implementation_max_concurrent_activities()` refuses to start an
> `implementation`-role worker whose N exceeds the mirrored width while the
> mutex still guards `run-implementer` — naming both numbers rather than
> silently clamping one that would then lie about capacity. What keeps the
> mirror honest against the real file is
> `orchestrator/validate_manifest.py::check_implement_admission_is_safe`,
> which fails mctl-agents' own PR-validation CI on any disagreement,
> including the mutex moving off `run-implementer` without the mirror
> following.
>
> The ceiling lifts once the mutex itself moves: a follow-up mctl-gitops PR
> relocates `synchronization.mutex: mctl-agents-proposal-claims` from
> `run-implementer` onto `commit-and-push` — a step measured in seconds, not
> the two-hour implementation budget — after which `ARGO_IMPLEMENT_MUTEX_TEMPLATE`
> flips to match and N is restored to 3. `implement_outcome.py` also now
> records WHY a `pre_start` verdict never started — `lock_wait`,
> `unscheduled` or `unknown` — so the recovery plane (#353, mctl-api#294)
> can tell a capacity problem from a cluster one instead of collapsing both
> into the same unexplained `pre_start`.

## Rollout order (fail-closed)

The order matters and it is the reverse of the intuitive one. A worker
polling the new queue must exist **before** any workflow routes work to
it — otherwise activities are scheduled onto a queue nobody polls and sit
there until timeout.

1. **mctl-agents:** `--role` flag, queue constants, slot limits. Routing
   unchanged; `all` behaves exactly as today on the control queue, and
   also polls the execution queue that nothing schedules to yet. Safe to
   release alone.
2. **mctl-gitops:** second deployment `mctl-agents-worker-exec` with
   `--role execution`, and `--role control` on the existing one. Both
   poll; nothing yet schedules to the exec queue.
3. **mctl-agents:** wire the metrics exporter (D5) — *done, #252*, with
   the scrape, alert rule and dashboard following in mctl-gitops.
   Independent of the split and safe on its own, and it goes BEFORE the
   flip so the flip has a baseline to be read against rather than being
   the first thing the new dashboards ever see.
4. **mctl-agents:** flip `submit_and_wait` onto the exec queue behind
   `workflow.patched("exec-queue")` — *done, #251*. Limits unchanged
   here — pairing the flip with a capacity change would make a bad number
   indistinguishable from a bad flip.

   Three call sites, not the two #251 names: `dev_loop._run_cwft` (the
   funnel feeding investigate, approve, implement and the in-loop
   shepherd tick), `incidents.py`, and `reconcile.py`'s `_apply`. The
   third was included deliberately — a 35-minute Argo poll waiting on the
   shared gitops write mutex is exactly the long-held slot this split
   exists to move, and leaving it would keep the workload the split was
   built to remove.

   Preceded by replay infrastructure (#251 step A, PR #280), which did
   not exist despite the issue's verification section assuming it did.
5. Soak one full dev-loop, then tighten the control limits (D3) from real
   numbers.

   **Shipped in 1.39.0 (2026-09-03).** Steps 3 and 4 were in `main` from
   09-02 and 09-03, but production ran 1.38.0 until this release — so the
   flip only started routing anything at 21:16 UTC on 09-03. The
   distinction matters more than it looks: for those ~44 hours the exec
   worker was polling an empty queue while every activity still went to
   control, and a reader of steps 3–4 marked "done" would reasonably have
   assumed otherwise. Merged is not deployed.

### The pre-flip baseline, measured

Step 3 exists so the flip has something to be read against. Recording the
numbers here rather than leaving them in a dashboard, because the whole
point is that step 5 tightens limits "from real numbers" and those numbers
have to survive the session that took them.

Window: 2026-09-01 ~21:00 → 2026-09-03 21:00 UTC (~44 h), VictoriaMetrics,
task queue `mctl-dev-loop`:

| | |
|---|---|
| activities received | **809** |
| minimum free activity slots, whole window | **96 of 100** |
| max p95 schedule-to-start | **0.87 s** |
| `mctl-dev-loop-exec` activities received | **0** |
| `mctl-dev-loop-exec` free slots | 40 of 40, throughout |

**Read it as an idle baseline, not a healthy one.** The premise of this ADR
is that long `submit_and_wait` pollers fill a shared pool once 9–11 loops
run concurrently. Nothing close to that happened in this window: four slots
of a hundred were the deepest it ever went. So the baseline establishes
what quiet looks like and says nothing yet about contention — which is
exactly why step 5 must wait for load rather than for elapsed time. Two
more days of this would add no information.

The zero on the exec row is not a fault: it is step 4 not being deployed
yet, and it dates the "before" side of the comparison precisely.

Steps 1–3 are individually reversible. Step 4 is one-way for histories
that record the patch, which is why it is alone.

> **Note on step 4's blast radius**, given D4's amendment above:
> in-flight executions **stay** on the control queue for the rest of their
> lives — `patched()` memoizes, so an execution that predates the marker
> never adopts it. Only new executions route to exec. The changeover is
> attrition across the merge-watch window, not a cutover and not a
> mid-life migration. The exec worker has been polling since 09-01, so
> there was never a window where a routed activity had nobody to pick it
> up.

## Consequences

- Two deployments to keep in sync on image tag. The release pipeline bumps
  by `values_path`, which names one file — so step 2 must add the second
  path, or the exec worker silently pins to whatever tag it launched with.
  This is the same class of drift that left the shepherd on 1.25.0 for
  three weeks (#238), and it is the most likely way this ADR goes wrong.
- Memory doubles at idle (two 256Mi ceilings instead of one). At current
  scale that is noise against the Argo pods.
- Two workers in one process (`--role all`) forced two questions a single
  worker let us ignore, both handled in `run_until_signalled`:
  - **Shutdown.** The Python SDK installs no signal handlers, so SIGTERM
    used to end the process outright with no drain. Pre-existing, but a
    split whose purpose is rollouts should not leave rollouts ungraceful.
  - **A worker dying alone.** A bare `await worker.run()` propagated any
    failure and crashed the pod. With two workers that has to be
    arranged: if only the control worker's loop dies, the execution
    worker keeps the process alive and healthy-looking while reconcile,
    intake and every DevLoop go dark — and nothing restarts, because
    nothing crashed. The signal is raced against the run tasks and a
    worker's failure is re-raised. A `run()` that returns cleanly with no
    signal is fatal too: nothing raised, and that queue has still stopped
    being polled.
  - **Draining on the crash path is capped, on the signal path is not.**
    A signalled shutdown has Kubernetes holding the stopwatch — SIGTERM
    now, SIGKILL at the end of the grace period. A crash has neither, so
    an unbounded drain of the survivors can park the process in the exact
    state the crash exists to escape. Capped at 20 s there; the restart,
    not the drain, is the fix.
- **No `graceful_shutdown_timeout` is set, deliberately.** The SDK
  default of 0 means `shutdown()` cancels in-flight activities rather
  than waiting for them. That is the behaviour we want: `submit_and_wait`
  polls an Argo run for up to two hours, which no Kubernetes grace period
  will ever accommodate, and Temporal reschedules the cancelled activity
  on another worker — the Argo run itself is untouched and the retry
  resumes polling it from the heartbeat rather than resubmitting. A
  non-zero window would delay every rollout to no end. Recorded here
  because it has been raised twice as an oversight (codex P2, claude P3
  on #249); it is a choice, not a gap.
- `--role all` remains supported indefinitely, for local development and
  as the rollback target. It is not deprecated — and because it is the
  rollback target for a state where routing has ALREADY flipped, it runs
  a worker on both queues, not only the control one. An `all` process
  listening on one queue would leave patched executions with no poller
  until they time out, which is not a rollback.

## Non-goals

HPA and per-agent quotas/cost budgets (D6 and #152's "per-agent quotas"
bullet — both need D5's metrics first); splitting reconcile onto its own
queue (the third queue D7 adds is for admission of one operation, not a
further holding-time split); per-service capacity `M` (needs a distributed
counter, see ADR-010 / mctlhq/mctl-api#337); any change to what runs in
Argo.
