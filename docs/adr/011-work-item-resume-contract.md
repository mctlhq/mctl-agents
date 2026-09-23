# ADR 011 — `WorkItem` resume contract: `WorkContextRef` and the `resume` signal

> **Status:** proposed
> **Date:** 2026-09-19
> **Issue:** mctlhq/mctl-agents#267 (related: #264, #198)
> **Supersedes:** nothing. It extends ADR 009 sec. 1's field/owner table with
> one new block and reaffirms ADR 009 sec. 5's boundary; it does not reopen
> ADR 009 sec. 4's step-chaining rule (`:203-205`, `:796-805`), which stays
> scoped to one execution exactly as written.

## Context

Before this ADR, an investigator execution had exactly one identity: the
GitHub issue URL. `IssueRef` (`orchestrator/temporal/workflows/dev_loop.py`)
carried one field; `workflow_id_for` (`orchestrator/temporal/issue_ref.py`)
derived the Temporal workflow id from it; `run_issue_investigator.py` took
three flags with no notion of an execution identity at all. The de-facto
correlation key was the proposal slug on disk, which meant a task could not
outlive one execution — `dev_loop.py`'s own comment on the subject: "It is a
restart, not a resume — the new run re-investigates and waits for a fresh
approve signal."

mctl-api#227 is introducing a canonical `WorkItem` record, durable across
surfaces. This ADR is `mctl-agents`' half of that: a tolerant client mirror of
the contract, a way to correlate two `ContextSnapshot`s produced by two
executions of the same work item without mutating either, and a `resume`
signal that can pick a task back up on a different surface or as a different
actor without inheriting the previous actor's approval.

## Decision

### 1. `orchestrator/work_context/` — a client-side mirror, not a second store

A new package, structurally identical to `orchestrator/lifecycle/`
(ADR-010): `contract.py` (stdlib-only frozen dataclasses, tolerant
`from_payload` staticmethods, closed-vocabulary classification), `client.py`
(synchronous `urllib`, `Bearer $MCTL_TOKEN`, uncertainty returned as a value),
`rollout.py` (a four-stage `off | observe | enforce | only` switch,
`WORK_CONTEXT_ROLLOUT_MODE`, defaulting to `off`). mctl-api owns the durable
`WorkItem` row; this package owns no state.

Wire shape (amended by #452): the mirror reads mctl-api's `workitem/v1`
exactly as served — the `{schema_version, work_item, state_version,
latest_execution}` view, the states `active | waiting | completed |
superseded | archived` (terminal: the last three), and the execution ledger
from `GET /api/v1/work-items/{id}/executions`, read all or nothing. Any other
`schema_version`, state or vocabulary value is `WORK_ITEM_UNKNOWN`. The test
fixtures under `tests/fixtures/workitem/` are captured from mctl-api's own
handlers. The ledger read relies on two mctl-api properties: attempts are
dense `1..n`, and the executions route is unpaginated. If mctl-api changes
either one, every read of an item with executions turns `WORK_ITEM_UNKNOWN`
until this mirror follows.

### 2. `WorkContextRef` on `ContextSnapshot` — sibling correlation, not chaining

A new frozen dataclass in `orchestrator/context_snapshot.py`:
`WorkContextRef(work_item_id, work_item_revision, execution_id,
execution_sequence, prior_execution_ids, resumed_from_snapshot_id,
origin_surface, current_surface, actor_kind, actor_id, surface_transition)`,
wired into `_SNAPSHOT_KEYS`, `to_dict`/`from_dict`, `_content_payload` (so it
participates in `content_hash` unconditionally, exactly as `step` does),
`validate()` and `to_log_dict()`.

This is deliberately **not** `StepRef`. ADR 009 sec. 4 scopes step chaining to
one execution and requires a child's `execution` block to equal its parent's
— a resume by definition produces a *different* execution, so reusing
`StepRef` would either violate that rule or make "same execution" stop
meaning anything. `WorkContextRef` is a second, independent axis: two
executions of one work item share `work_item_id` and are linked by
`prior_execution_ids` (and, optionally, the one-way pointer
`resumed_from_snapshot_id`), while each execution's `StepRef` chain (if any)
stays entirely inside its own `execution` block. Extending the parent-equality
check to require a step-child's `work_context` to equal its parent's keeps
both axes independently checkable, per ADR 009 sec. 4's own rule.

Extending ADR 009 sec. 1's field/owner table:

| Field | Owner | Notes |
|---|---|---|
| `work_context` | this ADR | `None` outside the work-context rollout; never read by an authorization decision (ADR 009 sec. 5, unchanged) |

### 3. Investigator flags — additive, keyword-only, lazily imported

`run_issue_investigator.main()` gains `--work-item-id`, `--execution-id`,
`--resume-from-execution-id`, `--surface`, `--actor-kind`, `--actor-id`;
`--issue-url` becomes conditionally required. `investigate()` gains the same
names as keyword-only parameters defaulting to `None`, so every existing
`investigate(url, tmp_path)` call site is untouched. `orchestrator.work_context`
is imported lazily, inside function bodies, preserving the module-scope
import discipline `run_issue_investigator.py` documents and
`tests/test_worker_isolation.py` enforces.

### 4. `reconstruct_canonical_state` — structured fields only

A pure function whose signature has no parameter capable of carrying a
transcript: a `WorkItem`, an optional proposal directory `Path` (read only
for the presence of the `requirements.md`/`design.md`/`tasks.md`/
`.status.yaml` artifacts and one scalar out of `.status.yaml`), and prior
execution digests in `ContextSnapshot.to_log_dict()` shape. `CanonicalState`
has no free-text field, mirroring how `ContextSource` is defended by having
no payload field to put one in.

### 5. The `resume` signal and `work_context` query on `DevLoopWorkflow`

`resume` parses defensively and never raises, exactly like `approve`. A
duplicate `execution_id` (the common case, since `execution_id_for` is a
deterministic hash of `work_item_id | sequence | attempt`) is a no-op; a
different `execution_id` arriving while one accepted resume is still pending
fresh approval is rejected (`reason="resume-already-pending"`); a mismatched
`work_item_id` is rejected (`reason="work-item-mismatch"`). An accepted resume
that changes surface or actor clears `_approved`/`_approver`, so **both**
`wait_condition(lambda: self._approved)` call sites in `run()` — the original
gate before the slug/approve-flip/implementer-release chain, and a second one
immediately before the implement CWFT is submitted — re-arm. The second gate
exists because the first one only proves someone had approved by the time the
loop *started* that chain; a resume landing in the real await-bounded gap
between the two gates would otherwise have no effect. Neither gate schedules a
new Temporal command when `_approved` is already true, so both are safe for
every pre-existing history: the investigate → implement command stream is
byte-identical when no `resume` is ever signalled.

### 6. Sealed snapshots are persisted in mctl-api (#431)

Owner decisions on #431: the durable record is mctl-api's existing Postgres
(mctl-api#362, insert-only, one snapshot per execution). The retry identity
is the store's execution id: the same content again is a replay, and
different content for the same execution fails explicitly. A human-input
signal continues the execution and seals nothing new. A second snapshot
exists only when the work-item layer creates a new execution.

`orchestrator/work_context/snapshots.py` is the client side, and
`context_assembly.assemble_investigator_context` calls it:

- **Before sealing**, `resumed_from` points `resumed_from_snapshot_id` at
  the snapshot the latest prior store execution sealed. The pointer is part
  of the sealed content. A failed lookup is logged and never blocks: the
  pointer is a convenience (§2).
- **After sealing**, `persist` sends the whole canonical document
  (`canonical_json(snapshot.to_dict())`, base64). The document carries the
  strategy name and version, and the store verifies its `sha256`. The
  policy checkpoint (ADR 014) decides immediately before the POST.
- **A retry is not a divergence.** Re-assembling the same execution's
  context yields a new `created_at`, so the bytes differ and the store
  answers 409. The stored document is then read back and compared without
  what a retry may change: `created_at`, the snapshot's own id and hash,
  each source's `retrieved_at` and `freshness.observed_at` (stamped with
  the assembly clock, so a retry at a later second restamps them), and
  `work_context.resumed_from_snapshot_id`, since the prior lookup is
  best-effort and can succeed on one attempt and fail on the next. If the
  rest is identical, the answer is a replay. Only a document that differs
  in anything else is a divergence, and the logged reason names the fields.
  The usual cause is that the live inputs changed under a retry (e.g. a
  new issue comment), not corruption. Recovery is a new execution. If the stored
  document cannot be read or decoded, the answer is UNKNOWN, never a
  divergence: a divergence is reported only once it is verified. The stored
  snapshot is never replaced.
- **Gating.** Nothing is sent unless `WORK_CONTEXT_ROLLOUT_MODE` is at least
  `observe` AND the execution id is a store execution (`we_...`).
  - A divergence blocks from `enforce` up, even under the
    `WORK_CONTEXT_REQUIRED=false` break-glass, because it means this
    execution already recorded a different context.
  - An unreachable or refusing store blocks where `blocks_on_unknown()`
    holds.
  - Blocking raises `SnapshotNotPersisted`. The investigator propagates it
    only in context-assembly mode `on`; in `shadow` it logs it, as it does
    every assembly failure.

### 7. The execution identity is the store's (#455)

Owner decision B on #431: the work-item store is the identity authority, and
nothing in the investigator invents an execution id. mctl-api keys an
execution by `(work_item_id, engine, engine_ref)` (`POST
/api/v1/work-items/{id}/executions`, 201 new, 200 the same one again), so:

- **A `we_...` `--execution-id`** was created by the work-item layer (e.g.
  the resume route). It must be in the item's ledger, and it is used as-is.
  Nothing is attached, and its phase stays with the layer that created it.
- **Otherwise the run attaches its own engine run** as `Running`: engine
  `MCTL_ENGINE` (`argo` by default, or `temporal`), engine ref
  `MCTL_ENGINE_REF`, else the Argo `WORKFLOW_NAME` (`{{workflow.name}}`).
  The returned `we_...` is `WorkContextRef.execution_id`, and its attempt is
  `execution_sequence`. A retried step of the same workflow gets the same
  execution back; a new investigation is a new workflow and gets a new one.
- **Any other `--execution-id`** (the dev_loop's `execution_id_for` seed
  hash) is logged as correlation and is never the identity.
- **No engine ref** means no identity, never a local substitute.
- **The prior list and the sequence** come from the ledger: the priors are
  the entries with a lower attempt than this execution's, so a retry never
  counts its own row, and the sequence is the store's attempt.
- **At the end of the run** the attached execution is advanced to
  `Succeeded` or `Failed`, best effort: a failed advance is logged and never
  changes the result. A failure the engine retries under the same engine
  run (`MCTL_ENGINE_FINAL_ATTEMPT=false`, the CWFT's primary attempt ahead
  of its fallback) leaves the execution `Running`, because the store never
  reopens an ended execution.
- **Gating.** Nothing is attached below `observe`, nor in a dry-run. Both
  writes go through the policy checkpoint (ADR 014,
  `attach:work-item-execution`). Without an identity the run has no work
  context at all (`WorkContextRef` cannot carry an empty id): at `observe`
  it proceeds and says so, and from `enforce` up it stops. A definite
  refusal (a foreign `we_...`, no engine ref, a policy DENY, an ended
  execution) stops it where the new answer may veto. An unanswered one (the
  store is down, another execution is active, the ledger moved) stops it
  where `blocks_on_unknown()` holds.

**Who holds the non-terminal slot.** mctl-api allows at most one non-terminal
execution per work item. Today the dev loop records no store execution, so
when the investigator attaches its Argo run the slot is free. Once
mctl-agents#461 dispatches work-item execution requests, the dispatcher
creates the execution when it fulfils the request and hands its `we_` down.
The investigator then uses it as-is (case a) and does not attach a second
one. No layer may hold an open execution while a child layer attaches its
own: that is a 409 `execution_active`, UNKNOWN, and refused from `enforce`.

**Residual.** An attach whose answer is UNKNOWN (a transport error after the
request was written, or a 2xx that does not describe our run) is not recorded
as held, so this run never advances it. A primary/fallback pair self-heals,
because it shares the engine ref. Any other stranded `Running` row has to be
closed by the engine's exit path. Closing it blindly from here would instead
create phantom ended rows on a plain 503.

### 8. Execution requests are dispatched by the platform (#461)

A surface asks for a run by creating an mctl-api execution request
(mctl-api#368); it never names the engine, the run or the execution. The
dispatcher in `orchestrator/temporal/dispatcher.py`, running in the worker
as the service principal, turns the request into a run:

1. **Claim** under a lease (mctl-api's CAS; a lapsed lease is claimable
   again under a new token, and fulfil/reject are fenced by the token).
2. **Decide from the store.** v1 runs the investigator for an item bound to
   a mctlhq GitHub issue; any other item is rejected `no_runnable_target`.
   A live DevLoop for the item (the latest Temporal execution in its ledger,
   or the issue-keyed loop) refuses a `start` (`loop_active`) and a
   `resume` (`resume_onto_live_loop_unsupported`, below). With no live loop,
   a `resume` starts a continuation exactly as `start` starts a first run.
3. **Start** `DevLoopWorkflow` under `dev-loop-<request id>`: `USE_EXISTING`
   on conflict, `REJECT_DUPLICATE` on reuse. A request is run once.
4. **Fulfil** with `(temporal, <that workflow id>)`. mctl-api attaches the
   `we_` (`start`: Running; `resume`: Pending under the `/resume` rule,
   which re-decides the item and its approval is the new run's own).

Start precedes fulfil so that every crash converges: before the fulfil, the
next claim derives the same workflow id and engine ref; after it, the loop
reads its `we_` from the fulfilled request itself (the
`bind_dispatched_execution` activity), which also refuses a request of
another item, an item about another issue, or an execution that is not the
loop's own engine run (`work-item-mismatch`). The loop passes
`work_item_id` and the `we_` to the investigate CWFT's declared parameters;
the investigator uses it as-is (case a in §7). The dispatched execution is
the first investigator run: the loop advances it to `Succeeded`/`Failed`
when that run ends, which frees the item's non-terminal slot for a later
resume. Human-input continuations run without it (their context differs,
and one execution seals one snapshot).

**Resume onto a live loop is refused in v1**, not delivered. The `resume`
signal carries the resumed execution's `we_`, which exists only after the
fulfil, and a fulfilled request is never claimable again: a crash between
fulfil and signal would lose the resume with nothing left to retry it, and
the resumed execution would stay `Pending`, blocking the item. Delivering it
durably needs a record of "fulfilled, not yet delivered" outside the
dispatcher process, which this ADR has not decided (Alternative 3 below
rejects a workflow per execution).

Off by default (`EXECUTION_REQUEST_DISPATCHER`). Claim, fulfil and reject go
through the policy checkpoint (ADR 014) and each dispatch step prints an
`EXECUTION_REQUEST_DISPATCH` audit line with the request, item, workflow
and execution ids. The dispatched path in `DevLoopWorkflow` is guarded by
`workflow.patched("execution-request-dispatch")` and replayed from
`tests/fixtures/histories/dev_loop_dispatched.json`.

**Known gap.** Everything else that names a DevLoop derives the
issue-keyed id (`workflow_id_for`): the investigator's approve instructions
on the issue, the shepherd's legacy liveness check and the orphan sweep. A
dispatched loop is `dev-loop-<request id>`, so it is approved through the
mctl-api approve route with its own id (it is in the item's ledger as the
execution's `engine_ref`), and the legacy checks do not see it.

## Alternatives

1. **Reuse `StepRef` for cross-execution chaining.** Rejected: it would
   require relaxing ADR 009's frozen "child's execution block must equal its
   parent's" rule, which would make "same execution" unfalsifiable. See
   Decision §2.
2. **Put resume state in the proposal's `.status.yaml`.** Rejected for the
   same reasons ADR-010 §1 gives for not putting ownership there — chiefly the
   gitops-commit mutex latency, which is useless as concurrency control for
   two surfaces resuming at once. A resume must also work before any proposal
   exists.
3. **A second Temporal workflow per execution, parented to a work-item
   workflow.** Rejected: it doubles the workflow-id space and the replay
   surface for no gain the `resume` signal does not already provide, and
   introduces a second orchestration engine this ADR explicitly avoids.

## Non-goals

- Any surface adapter (Telegram, web, ...). This ADR defines what a surface
  must send; it implements no surface.
- Any UI or trace-view rendering. `work_context` and `to_log_dict()` are the
  seam; the viewer is #195/#199 work.
- Shared mutable conversation memory of any kind.
- Cross-surface privilege inheritance — the opposite: a surface transition
  explicitly clears approval.
- Server-side `WorkItem` storage, revisioning or concurrency control
  (mctl-api#227's job).
- Wiring `work_context_params()` into `investigate_params`. The helper exists
  and is unit-tested; there is no call site, because doing so from inside
  `DevLoopWorkflow.run` would require reading `WORK_CONTEXT_ROLLOUT_MODE`
  directly in workflow code, which this codebase's convention (rollout state
  is read only inside activities, never inside `@workflow.defn` code — see
  every `orchestrator/temporal/activities/lifecycle*.py` rollout read) treats
  as a determinism risk not worth taking for a merge that mctl-api#335 and
  mctlhq/mctl-gitops#1279 would reject at the default `off` mode anyway.

## Platform impact

**Migrations.** None in this repository. `ContextSnapshot.work_context`
defaults to `None`; the module has no production producer yet
(`context_snapshot.py`'s own docstring). The one durable artifact affected is
the golden fixture `tests/fixtures/context/investigator-snapshot.json`,
re-cut in this change with `work_context: null`, which moves its
`content_hash` once.

**Backward compatibility.** Every new CLI flag defaults to `None`; every new
`investigate()` parameter is keyword-only; `IssueRef.work_item_id` defaults to
`None` so pre-existing history deserializes; the `resume` signal and
`work_context` query are additive and schedule no Temporal command by
themselves. `tests/test_workflow_replay.py`'s existing `dev_loop_full`
pre-patch fixture — which never signals `resume` — continues to replay
against the current workflow definition unchanged, because nothing about this
ADR alters the command stream for an execution that never receives a `resume`
signal.

**Resource impact.** Negligible. At `observe` and above, one extra
`GET /api/v1/work-items/{id}` per investigator execution; the new package adds
no dependency to `pyproject.toml`.

**Rollout default `off`.** Nothing in this ADR calls the work-item store or
changes CLI/workflow behaviour until an operator moves
`WORK_CONTEXT_ROLLOUT_MODE`, which is what allows this to merge ahead of its
two cross-repo prerequisites.

## Implementation map

| Concept | File |
|---|---|
| `WorkItem`, `WorkItemRef`, `SurfaceRef`, `ActorRef`, `ExecutionRef`, `CanonicalState`, `execution_id_for`, `reconstruct_canonical_state`, `work_context_params` | `orchestrator/work_context/contract.py` |
| `WorkItemClient` | `orchestrator/work_context/client.py` |
| `WORK_CONTEXT_ROLLOUT_MODE` ladder | `orchestrator/work_context/rollout.py` |
| `WorkContextRef` | `orchestrator/context_snapshot.py` |
| `--work-item-id` / `--execution-id` / `--resume-from-execution-id` / `--surface` / `--actor-kind` / `--actor-id`, `_work_context_from_args` | `orchestrator/run_issue_investigator.py` |
| `resume` signal, `work_context` query, `IssueRef.work_item_id`, `ResumeRejection`, `WorkContextState` | `orchestrator/temporal/workflows/dev_loop.py` |
| Golden fixture | `tests/fixtures/context/investigator-snapshot.json` |
| `persist`, `resumed_from`, `SnapshotAnswer` (#431) | `orchestrator/work_context/snapshots.py` |
| `WorkItemClient.seal_snapshot` / `execution_snapshot` | `orchestrator/work_context/client.py` |
| `resolve_identity`, `engine_ref_from_env`, `answer_from_attach` (#455) | `orchestrator/work_context/executions.py` |
| `WorkItemClient.attach_execution` (#455) | `orchestrator/work_context/client.py` |
| Execution-request contract mirror, typed reject reasons (#461) | `orchestrator/work_context/execution_requests.py` |
| `WorkItemClient.claim_execution_request` / `fulfil_execution_request` / `reject_execution_request` / `execution_request` (#461) | `orchestrator/work_context/client.py` |
| Dispatcher, `EXECUTION_REQUEST_DISPATCHER` (#461) | `orchestrator/temporal/dispatcher.py`, `worker.py`, `cli.py dispatch-once` |
| `dispatched_workflow_id`, `start_dispatched_dev_loop` (#461) | `orchestrator/temporal/start.py` |
| `bind_dispatched_execution`, `advance_dispatched_execution` (#461) | `orchestrator/temporal/activities/execution_requests.py` |
