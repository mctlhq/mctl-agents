# Worker rollout and the forward-only rule

Tracker: mctlhq/mctl-agents#1007 (see also mctlhq/mctl-gitops#1033). Status: **draft
consolidation, nothing in this document has been executed in production**. It folds the
procedures that were scattered over the issue comments of 2026-11-04 to 2026-11-06 into one
place and says, for every claim, what it rests on.

## Evidence labels

| Label | Meaning |
|---|---|
| CODE | read from the source in this repository or in mctl-gitops, at the commit named |
| LOCAL | measured on a local Temporal dev server (Server 1.29.1 to 1.32.0, Python SDK 1.34.0), synthetic data |
| KIND | measured on a throwaway kind cluster on a laptop (arm64, 2 CPU VM), images built from source, egress blocked |
| PROD | observed on the production system, read-only |
| LIVE | read from GitHub `main` on 2026-11-08 |
| UNVERIFIED | not exercised anywhere. Do not rely on it; treat a step that needs it as a STOP |

An UNVERIFIED cell is not a PASS. A read that fails, is partial or cannot be parsed is an
unknown state, never "none found".

## Scope

Three Deployments run the DevLoop workers on the `mctl-dev-loop` task queue family and move
together (the `mctl-agents-workers` gate in mctl-gitops `platform-gitops/release-gates.yaml`,
LIVE):

- `mctl-agents-worker` (control; also hosts the execution-request dispatcher when
  `EXECUTION_REQUEST_DISPATCHER` is `"true"`, LIVE values.yaml line 139)
- `mctl-agents-worker-exec`
- `mctl-agents-worker-implement`

The hazard this runbook exists for is the patch marker `deferred-resume-contract`
(`DEFERRED_RESUME_PATCH` in `orchestrator/temporal/workflows/dev_loop.py`, CODE). Once a worker
that contains it (called M1 below) advances a workflow past the marker site, the history carries
a `core_patch` marker. An older worker (1.117.0, no marker support) replaying that history fails
the workflow task with `[TMPRL1100] Nondeterminism error`.

## Rules in one place

1. **Forward only.** Never run a worker image that cannot replay histories carrying a recorded
   patch marker once such a history may exist. After M1 has run, the floor is the M1 build; a
   defect in M1 is fixed by a newer M1-compatible build, not by an older image.
2. **Image floor.** The `min_tag` of the `mctl-agents-workers` gate is the lowest tag any of the
   three files may carry. LIVE it is `1.117.0`; the pull request that deploys M1 raises it to the
   M1 tag, and from then on a tag below it is refused (see "Image floor").
3. **Group moves together.** The three files carry the same tag and move in one commit. A partial
   bump, a differing tag or a different `image.repository` is refused by the gate.
4. **A merge ships nothing by itself, a rollback is a reviewed change.** The shared rollback
   workflow template refuses the three gated files, so rolling a worker back takes a reviewed
   pull request that moves the group (CODE, mctl-gitops `release-gates.yaml` header).
5. **Unknown stops.** Any unreadable check, any `TMPRL1100`, any poller of an unknown build on
   the queue, or a missing owner decision is a STOP.

## Normal upgrade

Preconditions (each must hold; any UNVERIFIED one is a STOP):

| Precondition | State on 2026-11-08 | Evidence |
|---|---|---|
| Control worker uses `strategy: Recreate` (mctl-gitops#2394) | not merged, Draft; LIVE values.yaml has no `strategy` | LIVE |
| Image pre-pulled by digest on all eligible nodes before the old pod goes away | designed and rehearsed in kind, not applied | KIND, UNVERIFIED in production |
| Release-deploy gate (partial bump, differing tags, floor) | merged and exercised on a throwaway repository on real runners (12 cases) | LIVE, dry run |
| Marker scan (`tools/temporal_marker_scan.py`) over all running DevLoop workflows: zero UNKNOWN | one production run 2026-11-06: 0 PRESENT, 25 ABSENT, 0 UNKNOWN, listing complete. Stale; must be re-run immediately before the deploy | PROD (point in time) |
| Deadline-in-gap behaviour implemented (a watch whose deadline passes in a gap must not end on a stale `OPEN`) | not implemented | UNVERIFIED |
| Alert on `TMPRL1100` active | not present in this repository; the platform rule would live in mctl-gitops | UNVERIFIED (see "Detecting non-determinism") |
| Candidate commit frozen, replay tests re-run on it | not frozen | UNVERIFIED |
| Release hold lifted by the owner | hold is ON | LIVE |

Sequence once the preconditions hold (designed in the tracker comments of 2026-11-04 and
2026-11-06, not executed):

1. **Dispatcher off on the old image.** One reviewed values change sets
   `EXECUTION_REQUEST_DISPATCHER` to `"false"`; the pod restarts. Verify by pod identity that the
   control pod actually restarted with the flag off. There is no runtime switch: pausing is a
   restart (CODE).
2. **Drain.** Wait until no execution request is `claimed` with a live lease and no loop has an
   open delivery. Lease default is 120 s and the delivery wait is 60 s (`DELIVERY_TIMEOUT_SECONDS`,
   CODE), so at least 180 s; the explicit zero-claim check is the gate, not the timer. An
   unreadable answer counts as not drained. The read-only check is
   `mctl_list_work_item_execution_requests` plus the loop query `holds_execution_request`.
3. **Marker scan.** Run `python tools/temporal_marker_scan.py ...` (read-only). UNKNOWN or an
   incomplete listing blocks. PRESENT on a pre-M1 fleet is not expected.
4. **Image change.** One reviewed change that bumps all three files to the M1 tag, adds the
   pre-pull, and raises `min_tag` to the same tag. `Recreate` stops the old control pod before
   the new one starts.
5. **Verify the new poller.** Confirm by pod identity that the old pods are gone, wait, then
   `temporal task-queue describe` shows only the new identity on all three queues. A listing of
   terminated pollers can persist for a while and is not proof by itself.
6. **Dispatcher on.** A separate reviewed change sets the flag back to `"true"`.
7. **Watch** for the signals in "Detecting non-determinism" and "Stuck workflows" for at least
   the lease plus delivery wait; then re-run the marker scan.

Measured behaviour of a worker gap (KIND unless stated):

| Item | Result |
|---|---|
| Recreate gap with the image already cached, 6 rollouts both directions | rollout to first poll 6.6 to 11.8 s, median about 10.7 s; start-up dominates |
| Pull of the published image without a cache | 194 to 209 s on a laptop network. A new release is uncached by definition, hence the pre-pull |
| Production pull times for 500 MB to 1 GB images (kubelet histogram, 30 days, extrapolated) | about 96 % within 60 s, about 3 % over 180 s, slowest bucket 300 to 360 s (PROD, not specific to this image) |
| SIGTERM to process gone, idle / 5 running activities | 1.5 to 2.0 s / 1.3 to 8.4 s, exit 0, no SIGKILL. Other activity types and production: UNVERIFIED |
| Start, signal and Update-with-Start during a 20 s gap | all processed; delivery fulfilled at about 20 s (LOCAL) |
| Same during a 90 s gap | delivery deferred at 60.0 s (`update-timed-out`); the admitted Update is accepted 0.3 to 1.8 s after the worker returns; the run converges at 121 to 130 s (lease bound); one Update, one execution (LOCAL) |
| Temporal server restart of 17 s | parked loop and worker survive; an in-flight delivery returns `deferred` with a free-text reason; converges at about 124 s with one execution. Whether an admitted but unaccepted Update survives a server restart: UNVERIFIED |

## Forward-only rule: why and what it protects

- A history that carries the marker cannot be replayed by 1.117.0: `[TMPRL1100] Nondeterminism
  error: Change id deferred-resume-contract does not match expected id implement-queue` (LOCAL,
  reproduced on the rollback rehearsal in KIND). The workflow stays open, signals are kept, the
  task is retried.
- Rolling forward again recovers the run with the contract intact (KIND, LOCAL).
- A run created by continue-as-new carries the contract flag in its resume payload and has no
  marker of its own (CODE, `dev_loop.py`). A pre-M1 worker takes such a run silently, drops
  `deferred_resume_contract`, and the run stays legacy after a roll-forward (KIND). Every resume
  of such a chain answers `deferred: legacy-loop`; how long such a request stays `claimed` is
  UNVERIFIED. This is the silent form of the hazard and it is why the marker scan walks the
  continue-as-new chain: PRESENT if any ancestor has the marker, UNKNOWN if any ancestor cannot
  be read (including ancestors past the 30-day retention).
- Worker Versioning (pinning a build per workflow) would make a rollback below M1 possible, but
  it is a separate design, not a prerequisite for M1 under the forward-only policy. Findings from
  the local experiment: unversioned to versioned transition works for marker-free histories;
  Worker Deployments worked on the dev server without extra flags; a pinned override of a marker
  history to the old build fails with `TMPRL1100` and recovers when the override is removed;
  continue-as-new keeps the pin, and auto-upgrade on continue-as-new risks losing the contract
  fields (all LOCAL). The production state of the Worker Deployments flags cannot be read
  from outside: UNVERIFIED.

## Failed new worker

What to expect (KIND, three runs, crash loop with a bad environment): the rollout never becomes
available; start and signal calls are accepted and queue (backlog 2 in the test); the workflow
stays RUNNING; nothing is lost. With the dispatcher on, a claim cannot be observed to be safe in
KIND; with the dispatcher's only host down, nothing claims.

What to do:

1. Keep the dispatcher off (step 1 of the upgrade) and do not turn it on.
2. Observe: pod status and restarts of the new control pod; `temporal task-queue describe` for
   the pollers on the three queues; the marker scan; worker logs. Do not rely on worker
   stderr for `TMPRL1100` (see below).
3. Choose by what the new pod failed on:
   - **Never started, no history written by it yet** (crash at start): reverting the image to
     the previous tag with `kubectl rollout undo` recovered intake in 9 to 12 s in KIND. This is
     only valid while the marker scan shows no PRESENT and no continued run exists.
   - **M1 already wrote markers** (any PRESENT, or a continued run): reverting below M1 is
     forbidden. Fix forward with a newer M1-compatible build, through the normal reviewed path.
4. **The fixed worker also does not start, and no earlier M1-compatible image exists.** This
   exact combination has not been exercised. It is inferred from the crash-loop and rollback
   rows above: workflows wait without failing, intake queues, nothing is claimed while the
   dispatcher host is down, and no rollback target exists, so the only exits are a corrected
   build or an owner-approved decision. Status: **UNVERIFIED as a scenario**. Do not scale
   pre-M1 pods in as a workaround, and do not cancel, terminate or reset workflows without a
   separate owner decision.
5. A deploy whose pre-pull fails or whose digest cannot be verified is a STOP with the old pod
   left running (KIND: Deployment and old pod unchanged; a revert took 90 to 92 s). Cost of the
   design: every worker deploy is two reviewed changes, automated bumps of the group are
   refused, and a failing pre-sync blocks syncs of the application until a new commit or a
   manual sync once retries are spent.

## Stuck workflows

Symptoms: a workflow stays open, workflow task failures repeat, no progress.

1. Identify. Run the marker scan with a query narrowed to the suspected workflow, and read the
   history for a `WORKFLOW_TASK_FAILED` event (see the detector below).
2. Confirm which build serves the queue (pollers by pod identity).
3. Restore a compatible worker (the M1-compatible build). Remove any versioning override that
   points a marker-bearing run at an older build; repeating `TMPRL1100` is recoverable once the
   override is gone (LOCAL).
4. Late Update: an Update admitted while no worker was running is accepted by the loop on its
   own when a worker returns, and a redispatch of the same request id converges on one execution
   (LOCAL, real `DevLoopWorkflow` and dispatcher with the repository's fakes for Argo and
   mctl-api; real mctl-api lease timing: UNVERIFIED).
5. While a delivery is open (or a resume is pending), another user's `resume` is rejected with
   `resume-already-pending` and the rejection is terminal for that request id; a new request is
   accepted only after the first delivery ends. If the first is stuck because mctl-api does not
   answer, the window is unbounded (the advance retries every 10 minutes while the loop runs),
   and the existing operator `abandon` signal is the recovery. Safe check before retrying:
   the first request is `fulfilled`, no `claimed` row, `holds_execution_request` is false
   (CODE, LOCAL).
6. A merge-watch whose absolute deadline passes during a gap ends COMPLETED without a final
   poll and reports the pre-gap `OPEN` (LOCAL, host processes, shortened constants). A pull
   request merged inside the gap would go unseen by that loop. The fix is tracked separately
   and is **not implemented**; until it is, a gap that can span the end of a merge-watch is a
   reason to STOP.

## Stopping a rollout

- Before step 4 of the upgrade nothing has changed on the workers: stop and leave the
  dispatcher setting as it was found.
- During the image change: do not revert to an image below the floor. If the new pod is not
  Ready, follow "Failed new worker". If the pre-pull step has not completed, the old pod keeps
  running (KIND).
- After the dispatcher is turned off, turning it back on is its own reviewed change; an
  interrupted rollout must not leave it off silently. Record the state in the tracker.
- Never lift the release hold, change a flag or deploy as a side effect of stopping.

## Image floor (`min_tag`)

- Lives in mctl-gitops `platform-gitops/release-gates.yaml`, group `mctl-agents-workers`,
  with `repository: ghcr.io/mctlhq/mctl-agents` so a different image at a high tag does not
  satisfy it. LIVE value: `1.117.0`.
- The release-deploy and PR validator refuse (exit 6) when the new tag or any current tag is
  below `min_tag`, when the group is bumped partially, when tags differ, when a listed file or
  required key cannot be read, or when the gate itself cannot be read (an unreadable gate is a
  refusal, never a pass). The rollback template refuses the three files.
- Raising `min_tag` and loosening `requires` are reviewed pull requests to that file. The
  deploy pull request for M1 must raise `min_tag` to the M1 tag; until then a rollback to the
  pre-M1 image is not refused by the floor after M1 has run (LIVE, `min_tag` is 1.117.0).
- Tag grammar: `X.Y.Z`, ASCII, no leading zeros, no `v` prefix; a non-conforming tag is refused.
- What is not proven: main moving between the gate check and the push on a real runner;
  other deploy templates beyond the ones read; the floor value on the day M1 deploys.

## Detecting non-determinism

Worker stderr was **empty** during the rollback rehearsal in KIND, so a log-based alert would
have missed it. The failure is observable in three places; this repository contains the
history and metric checks as `tools/temporal_nondeterminism_scan.py` (tests in
`tests/test_temporal_nondeterminism_scan.py`).

| Surface | What it looks like | Evidence |
|---|---|---|
| Workflow history | a `WORKFLOW_TASK_FAILED` event with cause `WORKFLOW_TASK_FAILED_CAUSE_NON_DETERMINISTIC_ERROR` and message `[TMPRL1100] Nondeterminism error: ...` | LOCAL (real recorded history is the test fixture); KIND |
| Temporal server metrics | counter `failed_workflow_tasks{failure="NonDeterministicError"}` and `service_errors_nondeterministic` | LOCAL on Server 1.32.0 (CLI 1.9.1). Name and label on the production server (1.31.0) and whether production scrapes it: UNVERIFIED |
| SDK core log | a WARN line `Failing workflow task ... [TMPRL1100]` was printed by the local worker process | LOCAL only; empty in KIND, so not a detection path |

Use of the tool (read-only, inputs are files):

```
temporal workflow show -w <id> -o json > history.json
python tools/temporal_nondeterminism_scan.py history history.json
python tools/temporal_nondeterminism_scan.py metrics scrape.txt
```

Verdicts are NONDETERMINISM, CLEAN or UNKNOWN. Exit 0 only when every input is CLEAN; UNKNOWN
(unreadable, partial or empty input, an event-id gap, a scrape without the liveness series)
exits 2 and outranks a detection (exit 3). A CLEAN history means "none recorded so far", not
"never".

**What is not covered here.** The platform alert rule that pages on
`failed_workflow_tasks{failure="NonDeterministicError"}` is a mctl-gitops change (observability
stack) and is not part of this repository or this change; whether production metrics reach a
rule engine is UNVERIFIED. Until it exists, the monitoring precondition of the upgrade is not
met.

## Open items and owners

| Item | State |
|---|---|
| Recreate on the control worker (mctl-gitops#2394) | Draft, not applied |
| Deadline-in-gap behaviour for the merge-watch | not implemented |
| Alert rule for `failed_workflow_tasks{failure="NonDeterministicError"}` in mctl-gitops | not written |
| Marker scan re-run immediately before deploy, and before any rollback decision | needs an owner decision per run |
| Freeze the candidate commit and re-run replay tests on it | not done |
| Raise `min_tag` to the M1 tag in the deploy pull request | not done |
| Production cached-rollout and busy-SIGTERM measurement | not done |
| Worker Versioning (rollback below M1) | separate design, not started by decision |
