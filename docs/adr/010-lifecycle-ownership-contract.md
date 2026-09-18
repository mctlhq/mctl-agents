# ADR 010 — `EntityRef`, `LifecycleOwnership` and `ExecutionClaim` contract

> **Status:** proposed
> **Date:** 2026-09-12
> **Issue:** mctlhq/mctl-agents#350 (phase 0 of mctlhq/.github#57)
> **Supersedes:** nothing. It *decides* the question ADR-006 §6.1 deferred to
> #213, and records why #213's answer did not hold.

## Context

ADR-006 §6.1 left one question open, in as many words:

> the ownership marker (a `.status.yaml` field vs a Temporal visibility query)
> is decided in #213 — the `.status.yaml` field is the default candidate
> because the sweeper already reads those files and has no Temporal client.

\#213 chose the query. `DevLoopWorkflow` records a boolean in workflow memory
and publishes it through `@workflow.query shepherd_in_loop`
(`orchestrator/temporal/workflows/dev_loop.py`), and the cron sweeper asks
mctl-api, which asks that query (`run_shepherd._dev_loop_owns`). That is a
reasonable answer to "is a DevLoop driving this proposal right now". It is not
an answer to "who is responsible for this PR", and the difference has since
produced five independently-filed issues:

- **#239** — a PR created by a direct implementer run can end up with no
  review-loop owner at all;
- **#292** — a repository can have a nominal owner that cannot execute the
  review-fix phase;
- **#334** / `mctlhq/.github#38` — a PR with no proposal has no owner until
  something adopts it, and nothing does;
- **#344** — `merge_owner` is ambiguous enough that a reviewer read an
  ownership label as merge authorization, and a deferred owner can go stale
  invisibly;
- **#240** — the owner's decision needs review findings aggregated across
  reviewers before it is correct.

### What ownership is made of today

Six mechanisms, none of which is a record of responsibility:

| Layer | Representation | Durable? | Read by |
|---|---|---|---|
| DevLoop claims a proposal | `self._shepherd_in_loop` + `@workflow.query` | **No** — dies with the execution | the sweeper, over HTTP |
| One loop per issue | Temporal workflow id + `USE_EXISTING` (`temporal/start.py`) | Yes | Temporal only |
| Implementer holds a proposal | `.status.yaml` `attempt{id,started_at,expires_at}`, 130 min (`run_implementer.py`) | Yes | `_attempt_is_fresh` — which checks only `expires_at`, never the holder |
| Repo → steward vs shepherd | `SHEPHERD_SKIP_SERVICES` / `SHEPHERD_FIX_ONLY_SERVICES` in the shepherd CWFT, and `pr-steward.config.json` in `claude-remote/values.yaml` | Yes | `_service_mode` / the steward tick |
| Merge handed off | `.status.yaml` `merge_owner` | Yes | **nothing** — write-only |
| Owner-less PR detected | `OrphanSignal` (`activities/orphans.py`) | No | **nothing** — logged only |

Three properties of that table matter more than any individual row.

**The load-bearing check fails open.** `_dev_loop_owns` returns `False` on a
missing token, a non-https URL, a 404, a network error, a malformed URL, and on
every proposal left unanswered when the 60-second `DEV_LOOP_LIVENESS_BUDGET_S`
expires. `False` means "not owned", which means "sweep it". So the sweeper's
claim to ownership is *absence of evidence*, not evidence of absence, and the
system cannot distinguish "nobody owns this" from "I could not find out".

**The partition between the two drivers is a comment.**
`claude-remote/values.yaml` asserts *"steward owns ONLY what the shepherd
skips"*. Nothing enforces it; two lists in two repositories are kept in sync by
hand, and `mctl-academy` is already a third case that fits neither.

**There is no epoch, no fencing token, no compare-and-set and no
`last_progress_at` anywhere in `mctl-agents` or `mctl-api`.** The one lease that
exists is wall-clock only and its holder identity is written but never compared.

### The distinction this ADR is built on

```text
Entity + lifecycle phase
        ↓
LifecycleOwner      durable responsibility for reaching a terminal state
        ↓
ExecutionClaim      temporary right for ONE worker attempt to act
        ↓
result / progress / handoff / terminal
```

And, separately from both:

```text
owner/executor  = who is responsible / who is acting
policy          = whether the action is allowed at all
approval        = whether this exact consequential action is authorized
merge policy    = whether this PR may be merged
```

**Ownership never implies authorization.** This sentence is in the same section
as the fencing rule below, deliberately: #344 records a reviewer reading
`merge_owner` as an authorization to merge, which is exactly the mistake a
future consumer would make in code rather than in a review comment.

## Decision

This is a documentation-only change. No code, schema or manifest ships with it.

### 1. Where durable ownership lives

> **mctl-api Postgres is authoritative for *who may act*. GitHub and
> `.status.yaml` remain authoritative for *what the entity is*.**

`.status.yaml` gains a one-directional, read-only `ownership:` projection for
human audit and break-glass, which is never consulted for a compare-and-set, an
authorization, or an ownership decision (§7).

#### Why not keep it in `.status.yaml`

This was ADR-006's default candidate, and it is not merely the weaker option —
it cannot express this pilot. Four independent disqualifiers:

1. **The proposal-less path has no `.status.yaml`.** #334 says *"Do not
   synthesize a fake roadmap/proposal merely to fit today's discovery model"*
   and #351 repeats it. A store keyed on
   `agents-state/<service>/proposals/<slug>/` cannot represent a PR that has no
   proposal, so option (b) fails one of the four pilot paths by construction.
2. **The compare-and-set would be minutes deep.** The worker "has no gitops
   checkout and no deploy key by design" (`workflows/reconcile.py`), so its
   findings become writes only through Argo; every write goes through an
   Argo `commit-and-push` step under the single global
   `mctl-gitops-main-writes` mutex, and `workflows/reconcile.py` sizes its own
   wait on that mutex at **35 minutes**. The entire window between "I decided I
   own this" and "the record exists" is the double-drive window this epic
   exists to close.
3. **Git's non-fast-forward rejection is not a per-record CAS.** The write is
   clone → mutate → commit → push, and the standard recovery from a rejected
   push is rebase-and-retry, which reapplies the loser's edit on top of the
   winner's — converting a detected conflict into a silent lost update. That is
   the defect `proposal_state._write_status_atomic` already documents and
   \#354 now tracks. Building fencing on it would make an untracked live race
   the foundation of the safety property.
4. **The pr-steward could not participate.** It is a headless `claude -p`
   process in the claude-remote pod with a GitHub App token and no gitops write
   path. Making #292's nominal owner a first-class owner under option (b) means
   giving it a gitops clone and a turn at the same contended global mutex.

#### Why not move everything to Postgres

Demoting `.status.yaml` wholesale is the opposite error. It is the input to
`_discover_refs`, to `human_approval_satisfied`, and to the entire proposal
state machine. Moving that to mctl-api would put mctl-api on the critical path
of the approval gate and would be the "competing lifecycle database"
`mctl-api#293` forbids.

#### Why the split does not violate the epic's invariants

Two constraints look like blockers and do not bind, because they forbid
different things than this needs.

| Forbidden | What the ownership store does |
|---|---|
| a second **scheduler** (epic invariant 9) | no timer, no queue, no `due_at`, no `next_action`, no background sweep. It never calls GitHub, never submits Argo, never signals Temporal. `dead` and `stuck` are **derived on read** by the caller's existing tick (§4), never materialised — materialising either is precisely what would make it a scheduler. |
| a competing **lifecycle database** (`mctl-api#293`) | stores no proposal status, no PR state, no review finding, no merge decision. It stores one fact that exists nowhere today: *actor X holds responsibility for (entity, phase) at epoch N*. It duplicates nothing, so there is nothing to diverge from. |

This shape is already in production: `agent_executions`
(`mctl-api/internal/agentregistry/store.go`) is a mctl-api table written by a
Temporal activity (`activities/state.py record_execution`) recording a fact
about mctl-agents' own execution, and nobody calls it a second orchestrator.

The deciding property is #351's requirement that *"there must be one
authoritative decision for whether an entity is already owned."* Only a store
with a compare-and-set can **be** that decision, and four primitives already
running in mctl-api supply the semantics verbatim:

| Requirement | Precedent in mctl-api |
|---|---|
| at most one active owner per (entity, phase) | partial unique index, `internal/alerts/store.go` |
| atomic acquire | `INSERT … ON CONFLICT DO UPDATE … WHERE` on the existing row's state; a conflicting acquire matches no row, returns none, and re-reads to name the winner. **Not** `DO NOTHING RETURNING`, which yields zero rows and tells the loser nothing |
| atomic multi-statement transition | `pg_advisory_xact_lock(hashtext($1))`, `internal/agentregistry/store.go` |
| retry/replay idempotency | `UNIQUE (…)` + `ON CONFLICT DO UPDATE`, same file |

### 2. `EntityRef`

```text
EntityRef
  kind     "devloop-proposal" | "pull-request"
  id       devloop-proposal → "{service}/{slug}"
           pull-request      → "{owner}/{repo}#{number}"
  version  pull-request      → head SHA
           devloop-proposal  → .status.yaml content hash ("" before first write)
```

`id` for a proposal is the `agents-state` path key, which is already the stable
identity in `_discover_refs` and `OrphanSignal`.

**`version` is NOT part of the ownership key.** It is a precondition on the
*claim* only. Ownership must survive a review-fix push — a new head on the same
PR is the normal case, not a handoff — while a claim must not. Expressing this
as a schema property rather than as discipline is what stops a future change
from quietly making ownership head-scoped.

**Ownership for the review phase attaches to the `pull-request` entity**, not to
the proposal. A proposal-backed PR carries `proposal_ref` as a correlation
attribute, not a second ownership row. This single decision is what makes all
four pilot paths structurally identical and satisfies "without synthesizing a
proposal" with no special case.

### 3. Phases are typed per entity kind

Answering #350's explicit question: per kind, with a closed server-side registry
of legal `(kind, phase)` pairs. Global typing would make `investigate` a legal
phase on a pull request.

| kind | phase | replaces | liveness bound | progress bound |
|---|---|---|---|---|
| `devloop-proposal` | `implement` | the 130-minute `attempt` lease | 130 min | 130 min |
| `pull-request` | `review-remediation` | `_dev_loop_owns` + skip-lists + `merge_owner` | 10 h | 48 h |

**Two bounds, because there are two different questions**, and an earlier draft
of this ADR collapsed them into one — which was wrong twice over (agy P2 ×2 on
`mctl-agents#356`).

*Liveness* asks: is the owner still there? It is answered by `last_seen_at`,
which **any** tick refreshes, including a poll that found nothing to do. Losing
this is what licenses another actor to take over.

*Progress* asks: is the work moving? It is answered by `last_progress_at`,
which only an effected change refreshes. Losing this licenses an **escalation**
and never a takeover.

Collapsing them produced two failures that the single 6 h bound made
unavoidable:

1. **It could not tolerate a missed tick.** Progress at T=0, ticks at T=4h and
   T=8h. If the T=4h tick is lost to a pod restart, the next opportunity is
   T=8h — but a 6 h bound declares the owner stale at T=6h. A bound that must
   survive one missed tick has to exceed the interval to the tick *after* the
   missed one, i.e. `2 × cadence`, not `1.5 ×`. Hence 10 h.
2. **A healthy owner on a quiet PR looked dead.** A PR waiting on human review
   or a slow CI run produces no state changes for hours by design, and this
   ADR forbids writing progress for a poll that observed nothing. So the
   correct behaviour of a healthy owner was indistinguishable from a crashed
   one, and the reconciler would take the PR away every 6 h from an owner that
   was working perfectly — thrashing the epoch and fencing the original worker
   out the moment the review finally landed.

The original concern that produced the single-bound design still holds and is
still honoured: **a heartbeat must not prove useful progress indefinitely.** The
resolution is that it no longer has to. A heartbeat proves the owner is alive,
which is all it was ever evidence of; the absence of progress is a separate
signal with a separate and much longer bound, and its remedy is to tell a human
rather than to hand the entity to another machine that will be just as stuck.

The cadence itself: `SHEPHERD_TICK_EVERY_POLLS = 8` against a 30-minute
`MERGE_POLL_INTERVAL`, so roughly **4 h**.

`deploy-watch`, `investigate` and `await-approval` are **reserved and not
implemented**. Naming them here without shipping them is deliberate — ADR-007
set the precedent of recording the gap between the sketch and what was built
rather than discovering it later.

### 4. `LifecycleOwnership`, `ExecutionClaim`, and derived state

```text
LifecycleOwnership
  entity, phase
  owner {type, id}        devloop-workflow | shepherd | pr-steward | reconciler | human-codeowner
  epoch                   fencing generation; increments on handoff and recovery
  state                   active | handing-off | released | terminal
  acquired_at
  last_seen_at            any tick refreshes this; proves the owner exists
  last_progress_at        only an effected change refreshes this
  progress_evidence
  entity_version          last observed head (informational on this row)
  proposal_ref            "" for proposal-less PRs
  policy_ref              which policy granted this ownership
  handoff_from/to, handoff_started_at
  released_at, released_reason
  temporal_workflow_id    correlation

ExecutionClaim            (phase 2 — mctl-agents#352, not #351)
  claim_id, entity, phase
  owner_epoch             FENCE
  entity_version          FENCE — the head this attempt is pinned to
  executor {type, id}, attempt
  lease_until             crash recovery only; NOT durable ownership
  idempotency_key, outcome
  state                   active | released | expired | fenced
```

**Nothing derived is stored.** All of it is computed on read:

```text
dead       := state = 'active' AND now() - last_seen_at     > liveness_bound(kind, phase)
stuck      := state = 'active' AND now() - last_progress_at > progress_bound(kind, phase)
conflicted := an active claim whose owner_epoch <> the current epoch,
              or whose entity_version <> the current head
```

Only `dead` licenses a takeover. `stuck` licenses an escalation — a human is
told, and ownership does not move, because handing a stuck entity to another
machine produces a second stuck machine and an epoch bump.

A second active owner is not in that list because it is impossible: the partial
unique index makes "at most one active owner" a property of the database rather
than of the code that writes to it.

**Progress means an effected state change, not a heartbeat.** A poll that
observes nothing new refreshes `last_seen_at` but writes no progress, and
`progress_evidence` records what counted. #350 requires that a heartbeat alone
must not prove useful progress indefinitely — which is satisfied by the
heartbeat feeding a different field with a different consequence, rather than by
refusing to record liveness at all.

### 5. State machine

```text
                       acquire (CAS insert)
          (none) ───────────────────────────▶ active ◀──┐ progress
             ▲                                 │  │─────┘ (CAS on epoch + owner_id)
             │                                 │  │
             │ acquire by next owner           │  └──▶ handing-off ──▶ active (epoch+1)
             │                                 │              │
        released ◀── release                   │              └─ incomplete beyond bound
             │                                 ▼                   → conflict (#353)
             └──────────────────────────── terminal
```

`terminal` is reached when the PR merges or closes, or the proposal reaches
`merged` / `rejected` / `review-stuck`.

**Recovery is the only path that takes ownership from a live row**:
`acquire(force_if_dead=true, expected_epoch=N, evidence=…)` re-checks
**liveness** — not progress — *server-side* under the advisory lock, bumps the
epoch, and writes a `recovered` event. A client may not assert that an owner is
dead; it may only ask the server to re-evaluate it.

### 6. The fencing rule, and where it is enforced

> Every mutating lifecycle action carries
> `(entity_kind, entity_id, phase, owner_epoch, claim_id, entity_version)`. The
> server accepts it only if the ownership row still has that `epoch`, the claim
> is `active` and unexpired, and the claim's `entity_version` equals the current
> head. Otherwise it returns **409 FENCED** and the worker **aborts without
> mutating**.

**The epoch check is an early filter, not the fence.** This is the correction
agy raised as a P2 on `mctl-agents#356`, and it is load-bearing enough to state
plainly rather than bury: asking mctl-api "am I still the owner?" and then
pushing to GitHub is a time-of-check/time-of-use race. Worker A validates,
stalls on a GC pause, its ownership moves, worker B acquires and pushes, and
A's delayed push still lands on top. No amount of checking *earlier* fixes
that, because the check and the mutation are against different systems.

So fencing has two layers, and only the second is authoritative:

| layer | mechanism | what it buys |
|---|---|---|
| early filter | epoch + claim validity via mctl-api, immediately before the mutation | a fenced worker stops before doing expensive or noisy work |
| **authoritative CAS** | the target system's own precondition — `git push --force-with-lease=<branch>:<expected sha>` for `apply_followup`, `gh pr merge --match-head-commit` for `merge_pr` | the mutation itself fails if the world moved, whatever the worker believed |

`merge_pr` already uses `--match-head-commit` today, for exactly this reason;
`apply_followup` currently pushes without a lease and must gain one. The
ownership epoch's real job is to arbitrate *responsibility*, and it can never be
the last word on a mutation in a system it does not control.

A fenced claim aborts **before** invoking git, and does **not** consume a
`review_attempts` slot — the existing transient/deterministic distinction in
`apply_followup` already has the right shape for this.

**Ownership grants neither push nor merge authority.** Merge remains gated by
`NEVER_MERGE_SERVICES`, `_service_mode`, branch protection and CODEOWNERS,
re-evaluated at the action boundary. A delegated executor (§9, path 3) inherits
the right to *act*, never the right to *merge*.

### 7. The `.status.yaml` projection

Postgres is written first and authoritatively. The projection is written
best-effort afterwards, rides gitops commits that already happen and adds none
of its own, and carries an explicit `# derived, not authoritative` marker.

**Rule, enforced in code and in test: the projection is never read for a
compare-and-set, an authorization, or an ownership decision.** The test mutates
the block to a contradictory value and asserts that no decision changes. Without
that test the projection is a second source of truth waiting to be consulted by
the next person who finds it convenient — which is how the current six-mechanism
situation arose.

### 8. Idempotency

```text
idempotency_key = sha256("{kind}|{id}|{phase}|{owner_epoch}|{attempt}|{version}|{action}")
```

Deterministic: no UUID, no wall clock. A Temporal activity retry, an Argo pod
restart and a replayed workflow all re-derive the identical key, hit the unique
index, and `ON CONFLICT DO UPDATE … RETURNING` returns the *same* row including
its recorded `outcome` — so the effective mutation is not repeated.

This replaces a real defect. `run_implementer.py` currently derives the attempt
id as `os.getenv("WORKFLOW_UID") or str(uuid.uuid4())`. The UUID fallback is
non-deterministic in exactly the case where determinism matters: a retried pod
with no workflow context. The claim path must **refuse to acquire** rather than
mint a random key; a deterministic fallback is acceptable, a UUID is not.

### 9. Temporal I/O runs in activities, never in workflow code

**No lifecycle API call may originate in `@workflow.defn` code.** Every acquire,
progress, release, terminal and handoff from `DevLoopWorkflow` goes through an
activity in `orchestrator/temporal/activities/lifecycle.py`; the workflow
schedules it and consumes a typed result.

Network I/O inside workflow code is non-deterministic and breaks replay, which
is the property the whole contract depends on — an ownership record that cannot
survive a replay is worse than none, because it would be trusted. A test asserts
this rather than leaving it to review.

Two transports, for two callers: async `httpx` in the activity, mirroring
`activities/state.py`; synchronous `urllib` in `run_shepherd`, reusing its
existing https pin and no-redirect opener, because that process is an ordinary
CLI and not a Temporal worker.

Ownership failure must never fail the workflow. If acquire fails,
`DevLoopWorkflow` declines the claim and lets the sweeper keep the PR — the same
fail-safe direction `_shepherd_is_pinned` already takes.

### 10. The four pilot paths

**1 — normal DevLoop PR.** The workflow acquires
`(pull-request, "{owner}/{repo}#{n}", review-remediation)` as
`owner_type=devloop-workflow` at the point whose own comment already says
*"between those two points this execution IS the owner, and the cron must
already be standing down"*. The sweeper sees a healthy owner and skips.

**2 — direct implementer PR (#239).** *Target state, not yet wired — it waits
on the `handoff/complete` caller in #353.* On PR creation the implementer
**handoff-starts** to `owner_type=shepherd`, which the next sweeper tick
completes by acquiring at epoch+1. Between those two points the row exists in
`handing-off`: a *deterministic* unowned state the reconciler can adopt, which
is what #351 means by "no silent zero-owner gap". The 15-minute cron bounds the
gap; an incomplete handoff is a #353 condition.

**3 — steward-owned repo (#292).** Policy resolves the steward as owner. When a
blocking review lands, the shepherd takes a **delegated claim under the
steward's epoch** rather than taking ownership. Ownership stays with the
steward, execution is delegated, merge authority is unchanged. This is the
cleanest demonstration that owner ≠ executor is not academic: it is the only
shape that makes #292 correct without either duplicating the implementer into
the steward or handing the shepherd merge rights.

**4 — proposal-less adopted PR (#334).** Identical row shape,
`proposal_ref = ""`, owner `reconciler` at adoption then handed to `shepherd`.
No `.status.yaml` is created and no proposal is synthesised. The *shape* exists
from phase 1; the *discovery* of adoptable PRs is #334's own work.

### 11. Access control

The write API decides who may mutate a PR, so its identity boundary is part of
the contract rather than an implementation detail:

- which principals may acquire, and which may only read;
- a caller may release or progress only an ownership row whose `owner_id` is its
  own identity;
- the sole exception is an explicit, audited recovery operation
  (`mctl-api#294`), which records why;
- none of this confers GitHub merge or approval authority.

### 12. Migration

The governing rule: **at every stage exactly one mechanism is authoritative, and
disagreement means "owned" (do nothing), never "free" (act).**

| mode | record written | new answer computed | who decides | mutation requires |
|---|---|---|---|---|
| `off` | no | no | old only | old says free |
| `observe` | yes | yes, logged only | **old only** | old says free |
| `enforce` | yes | yes | **either may veto** | a healthy owner record naming me |
| `only` | yes | yes | new only | a healthy owner record naming me |

`observe` measures divergence; it does **not** compose safety. The union begins
at `enforce` and not before. Saying otherwise would be the same category of
error as the current fail-open probe: a mechanism credited with a guarantee it
does not provide.

**One genuine behaviour inversion.** `_dev_loop_owns` fails open today. Under
the contract an unreachable store fails **closed for push and merge only** —
reads, projection and escalation stay permitted — because "I cannot tell whether
someone else owns this" must not license a second actor. `LIFECYCLE_OWNERSHIP_REQUIRED=false`
is the documented break-glass. This flips at `enforce`, after the soak.

**Bootstrap precedes the soak.** The store starts empty, so enabling `observe`
without importing current ownership would measure migration noise for a week
instead of divergence. Import is conservative — live DevLoop → `devloop-workflow`,
steward-owned repo per current policy → `pr-steward`, proposal-backed PR with no
live workflow → `shepherd` — and **anything ambiguous is left unowned or written
`conflicted` with evidence, never guessed.**

**The soak gate is time *and* sample**: at least N real ownership decisions
observed (N from the measured decision rate at bootstrap, floor 200) and zero
dangerous divergences, where dangerous means the new answer would have permitted
a mutation the old answer forbade. Seven quiet days prove nothing.

Per-mechanism migration:

- **skip-lists** are reclassified, not deleted. A skip-list is a statement about
  *policy* ("who should own this repo's PRs"), which survives; it was being read
  as *state* ("who is acting"), which moves. `_service_mode` and
  `_merge_owner_for` become `default_owner_for` and `merge_authority_for` in
  `orchestrator/lifecycle/policy.py`, and the resolved policy is recorded as
  `policy_ref` on every acquire so "why does this actor own it" is answerable
  from the row. Finally the two hand-synced lists collapse into one
  `lifecycle-policy.yaml` read by both shepherd and steward, with CI asserting
  the partition is total and disjoint.
- **the `attempt` lease** is dual-written through phase 1 and left as the sole
  lease until #352, where `_attempt_is_fresh` becomes the union of claim and
  yaml lease and later drops the yaml read.
- **`_dev_loop_owns`** becomes one batched read; the thread pool and the
  60-second budget disappear with it, because a single batched read needs no
  budget.
- **the `shepherd_in_loop` query stays** — demoted from *the* ownership answer
  to one input to the `dead` predicate in §4. It corroborates `last_seen_at`:
  an execution that Temporal still reports Running is evidence the owner exists,
  which is a liveness question and has nothing to do with `stuck`. Only `dead`
  licenses the recovery path in §5, so this query can no longer, by itself,
  decide that anyone may act.
- **`merge_owner` is retained, still unread, and documented as descriptive**
  (#344 item 1). The canonical answer is `merge_authority_for`.

### 13. Metrics

Shipped with the store rather than retrofitted: `owner_acquire`,
`ownership_conflict`, `store_unknown`, `divergence`, `claim_fenced`, `handoff`,
`stale_owner` (counting `dead`, since that is the one that licenses a takeover),
`orphan_adopted`. The soak gate is read from `store_unknown` and
`divergence`; a rollout whose safety criterion has no metric behind it is a
rollout judged by anecdote.

## Non-goals

- A generic distributed-lock service for arbitrary application code.
- Replacing Temporal or Argo orchestration, or adding a second scheduler.
- Making `owner` equivalent to RBAC, approval, or merge authority.
- Moving PR merge policy out of GitHub and repository configuration.
- Making every mctl resource implement this before the DevLoop pilot works.
- Fixing the `.status.yaml` lost-update race (#354). This contract routes
  *around* it by not putting ownership there; the remaining fields still need
  the lock.

## Implementation map

| Phase | Issue | Contents |
|---|---|---|
| 0 | #350 | this ADR |
| 1 | #351 | `LifecycleOwnership` only — store, API, client, writers, projection, `observe`, bootstrap |
| 1 | mctl-api#293 | read-only inspection, ships before the soak so rollout is diagnosable |
| 2 | #352 | `ExecutionClaim`, database-clock lease and renew, epoch fencing, handoff — **shipped** (Python contract, clients, call sites, rollout gate; the mctl-api routes ship separately) |
| 3 | #353 | reconciler over known ownership rows; adoption integration after #334 |
| 4 | mctl-api#294 | guarded operator recovery with optimistic preconditions |
| 5 | — | extension beyond DevLoop, gated on the pilot; hand-off to `mctlhq/.github#21` and `#42` |

Phase 1 acquires ownership only. No `ExecutionClaim`, no lease and no fencing
ship in #351; the existing 130-minute `attempt` lease is untouched until #352.

**Phase 2 (#352), as shipped in this repository.** `orchestrator/lifecycle/contract.py`
gained the claim types (`ExecutionClaim`, `Executor`, `ClaimAnswer`), the closed
verdict vocabulary and the single classifier `claim_answer_from`, plus
`idempotency_key_for`. `orchestrator/lifecycle/claim.py` is the synchronous
`ClaimClient` for `run_shepherd`/`run_implementer`, and
`orchestrator/temporal/activities/lifecycle.py` gained the `execution_claim`
activity for `DevLoopWorkflow`. Three open questions from requirements.md were
resolved as implemented, not merely proposed:

- **Wire status for a fence.** A 409 carrying `code: "fenced"` is `CLAIM_FENCED`;
  a 409 carrying `code: "claim-held"`, or any other 409, is `CLAIM_HELD_BY_OTHER`
  — never guessed toward either side on an unrecognised code, and never a
  licence to execute. One exception, and only one: a 409 whose claim record
  names the **asking** attempt is `CLAIM_HELD_BY_ME`. Determinism in §8 exists
  so a restarted pod re-derives the same identity and can retake the claim its
  killed predecessor never released; classifying that conflict as a rival's
  would leave the proposal stuck `in-progress` behind itself for the full
  lease, with the operator told a competing executor holds it — naming us. The
  comparison is `claim_verdict_for(claim, asking)`, so a free state on a 409
  does **not** qualify: a conflict answering "nobody holds it" is a
  contradiction, and resolving it toward vacancy is the one direction that
  licenses a second executor. A fence outranks both — the epoch moved, so even
  our own record is not a licence to continue.

  A retake is *completed*, not assumed. A 409 is a refused acquire, so the
  store never applied `lease_seconds`: what the client adopts is the dead
  predecessor's remaining `lease_until`, while the implementer stamps a fresh
  full-length `.status.yaml` lease moments later. That is the one path where
  the two leases desynchronise, and in the dangerous direction — the claim
  expiring before the run it guards, which answers `CLAIM_UNCLAIMED` to the
  shepherd's freshness check and lets a second implementer start against a
  live one. So `_acquire_claim` renews the adopted claim before returning a
  context (the renew this section's determinism exists for) and treats a
  refused renew as the refusal the 409 originally was — through the same
  rollout predicate as every other refusal, so the stages cannot drift. The
  acquire is logged `renewed`, never `acquired`: the store granted nothing, and
  the retake is the event worth seeing, because reaching it means a pod died
  holding a claim. The two `renewed` lines it produces — the remapped acquire
  and the renew itself — are told apart by the `op=` field the claim log
  carries, not by a seventh event: the vocabulary above stays closed.

  **A 2xx renew needs no record.** `/claims/renew` is the one claim route whose
  success is meaningful with an empty body, and the client reads a `204`, a
  `{"status": "renewed"}` or an `{"ok": true}` as `claim-held-by-me` with no
  record attached. A renew is addressed BY CLAIM ID by the actor already
  holding it, so the record resolves nothing the caller did not send, and
  nobody new is licensed; on `acquire`, where the record is the only thing
  naming the winner, a body-less 2xx stays the protocol anomaly it is. This is
  the claim-side counterpart of `/release` and `/terminal` answering
  `wrote-no-record`, and it is pinned here because the retake made `renew` a
  safety input for the first time: without it a store that answers renews
  body-lessly refuses the restarted attempt on every restart, under
  `LIFECYCLE_OWNERSHIP_REQUIRED`, until the orphan lease expires. The branch
  is narrow on purpose, and narrow by ALLOW-list on both axes: it admits an
  empty body, or one whose every key is an acknowledgement name (`ok`,
  `renewed`, `result`, `status`, `success`) carrying an affirmative value —
  literal `true`, or one of `accepted`, `active`, `held`, `ok`, `renewed`,
  `success`, `updated`, compared case-insensitively. Both sets are written out
  here because this section is the only place the renew response shape is
  pinned, and a server author guessing `{"status": "extended"}` or
  `{"ok": "true"}` would be refused. A store with anything more to say — the
  new `lease_until` above all — should answer with a FULL claim record, which
  is read the ordinary way; the tolerance here is for the bare
  acknowledgement, and a half-record (`{"status": "renewed", "lease_until":
  ...}`, no `claim_id`, no `state`) is deliberately neither: a partial record
  is the one shape this image cannot verify and must not assume.
  Everything else answers `claim-unknown` — a record this image could not read
  (a 200 error envelope, a record from a newer mctl-api, a record nested one
  level deeper), and equally an acknowledgement that says NO — `{"ok": false}` and
  `{"status": "expired"}` on the value rule, `{"reason": "lease already
  expired"}` on the key rule, since `reason` is no acknowledgement name at
  all. Reading the
  sets this way round is the point: a name nobody has thought of falls to
  `claim-unknown` rather than to the most confident verdict in the vocabulary
  (claude P2 on `b362b5e` and `f4d0dec`). A body that fails to parse
  at all — an HTML error page served with a 200 — is likewise `claim-unknown`,
  as before.
- **Deterministic attempt fallback.** `run_implementer._resolve_attempt_id`
  resolves `WORKFLOW_UID`, then
  `sha256("{service}|{slug}|{owner_epoch}|{attempt_ordinal}|{HOSTNAME}")`. The
  `attempt_ordinal` is fixed at `0` for the implement phase, since this
  repository's `.status.yaml` `attempt` block has no per-epoch attempt counter
  to derive a real ordinal from yet — a fixed ordinal still satisfies the
  determinism and renew-on-restart properties this fallback exists for. (The
  review-remediation phase does have one, `review_attempts`, and uses it.)
  `HOSTNAME` is in the digest because determinism must not become a collision:
  without it two pods working the same proposal in the same epoch derive the
  SAME executor id, each one's `acquire` reads as the other renewing its own
  claim, and the mechanism meant to stop concurrent implementers licenses
  them. In Kubernetes `HOSTNAME` is the pod name — stable across a container
  restart inside one pod, distinct across pods. Residual, stated rather than
  hidden: two processes on one host with no `WORKFLOW_UID` still collide; the
  fix for that shape is to set `WORKFLOW_UID`, not to mint a random id.
- **Lease durations.** `LIFECYCLE_CLAIM_LEASE_SECONDS_IMPLEMENT` (default
  `7800`, matching the yaml lease it dual-writes beside) and
  `LIFECYCLE_CLAIM_LEASE_SECONDS_REVIEW`, both env-overridable tunables, not
  contract. The review default is one `MERGE_POLL_INTERVAL` (1800s) as a
  FLOOR, widened to `IMPLEMENTER_TIMEOUT_SECONDS + 2 ×
  IMPLEMENTER_COMMAND_TIMEOUT_SECONDS` when those bound a longer run. Nothing
  renews a claim mid-run — `ClaimClient.renew`'s only production call is the
  retake in `_acquire_claim`, which runs once, before the run starts, and never
  again as a heartbeat — so a lease shorter than the run holding it expires
  under its own attempt and comes back from the push-site check as
  `CLAIM_UNCLAIMED`, standing the attempt down for a race that never happened.
  `_claim_lease_seconds` is therefore LENGTHEN-ONLY: an override below the
  computed lease is refused with a log line naming both numbers, and the
  computed lease is used. Commenting the variables out of `.env.example`
  documented that hazard; clamping removes it, so an active
  `LIFECYCLE_CLAIM_LEASE_SECONDS_REVIEW=1800` can no longer re-pin the floor
  the sizing above exists to widen (agy P2 on `0af3b38`, claude P3 on
  `b362b5e`). They stay commented out regardless, since a pinned value is
  still one more thing to keep in step by hand.

`run_implementer._push_followup` and the existing-branch path of
`_push_and_open_pr` now push with `--force-with-lease=<branch>:<sha>` — the
explicit-SHA form, never the bare flag — and both call `ClaimClient.check()`
immediately beforehand, aborting non-charging on `CLAIM_FENCED`
(`run_implementer.EXIT_FENCED`) and on a refusal — another executor holds the
entity, or the store could not answer under the ownership break-glass —
(`EXIT_CLAIM_REFUSED`). Both codes classify as `FollowupKind = "fenced"`: two
different events, one shepherd handling, charge nothing and say which. Three
verdicts reach that refusal and each is reported as itself rather than as a
rival executor: `CLAIM_HELD_BY_OTHER` (a real, named holder),
`CLAIM_UNKNOWN` (the store could not answer, failed closed under
`LIFECYCLE_OWNERSHIP_REQUIRED`) and `CLAIM_UNCLAIMED` (the hold this attempt
expected is gone — released, expired or fenced). In batch mode the same
refusal is a skip at both sites — never `_mark_needs_triage` — but not the
same skip. The acquire site exits before anything is spent, so it does not
charge the batch budget; the push site runs after a full model pass, so it
does (`counts_toward_limit` defaults True), otherwise one mctl-api blip per
proposal re-runs the model down the whole accepted queue. What happens to
`.status.yaml` follows the verdict, which the exception carries as an
attribute: `CLAIM_HELD_BY_OTHER` leaves the file to the live holder,
`CLAIM_UNKNOWN` fails closed and leaves the yaml lease to expire without
releasing a claim it cannot confirm, and `CLAIM_UNCLAIMED` hands the proposal
back — releasing and restoring `accepted` so the next tick retries instead of
waiting out a 130-minute hold that does not exist.

The hand-back is narrower than the verdict, on two axes. First, the verdict
alone does not prove the entity is free: `FREE_CLAIM_STATES` folds `fenced` in
beside `released` and `expired`, and `check` sends this attempt's own
`claim_id`, so a claim fenced server-side by a newer executor comes back as our
own fenced record and reads `CLAIM_UNCLAIMED`. That executor is running right
now and owns the `attempt` block. The exception therefore carries the raw
`claim_state` as well, and only `released` and `expired` — an answer that
proves nobody is there — hand back; a `fenced` record, or an answer that named
no claim at all, is handled like a live holder. Second, the restore is a
compare-and-swap on `attempt.id`: between this attempt's `in-progress` write
and the moment its claim turns out to be gone, a second executor can have taken
the proposal legitimately, and `update_status_yaml(..., attempt=None)` would
erase the block `_attempt_is_fresh` reads — letting a third implementer start.

That compare-and-swap is a property of every status write an ending attempt
makes, not of the hand-back alone, and lives in one predicate
(`_status_is_still_ours`) for that reason. The 409 `{"code": "fenced"}` form —
ADR-010 §6's primary fence encoding, as opposed to the 2xx `fenced` record —
answers `CLAIM_FENCED` and lands on `_mark_needs_triage`, which writes
`needs-triage` carrying the ending attempt's own block; unguarded, that is the
same erasure plus a human gate on a race the fence is explicitly not a failure
of. `_mark_needs_triage` therefore consults the same predicate whenever it was
given an attempt, returns whether it wrote, and releases the claim either way.
The same "do not act on what the store did not say" rule governs the release
event: only a 2xx from `/release` logs `released`, because a body-less 204 and
a plain `{"status": "released"}` land on opposite values of `accepted` while
both being writes that freed the claim.
That existing-branch push REPLACES the branch rather than adopting it: the
retry clones fresh and recreates the branch off the default branch, so the
dead attempt's commits are discarded. Intended — nothing references them —
but `--force-with-lease` proves only that the ref did not move, never that we
contain it. `run_shepherd._attempt_is_fresh` is the union of an active claim
and the yaml lease. The `attempt` block's `id` is read by the CLAIM branch
only; the yaml branch stays keyed on `expires_at` alone, because an unexpired
lease is a live hold whether or not a holder was ever recorded, and with no
`id` there is nobody to ask a claim about, so the yaml lease decides at every
stage including `only`. A `CLAIM_UNKNOWN` never frees an attempt: like
`claim.blocks_mutation`, it is gated on the `LIFECYCLE_OWNERSHIP_REQUIRED`
break-glass, so an unreachable store holds rather than releases.
`DevLoopWorkflow._watch_pr`'s `finally` still issues a bare `release` when the
watch ends non-terminally. It was briefly changed to `handoff-start` and
reverted inside this same change: `handoff-start` writes a HOLDING
`handing-off` state that only `/handoff/complete` resolves, and no caller of
that route exists in this repository yet (#353), so the write would have left
every abandoned watch stuck in `handing-off` — strictly worse than the
zero-owner gap a release leaves. There is deliberately no
`workflow.patched("lifecycle-claims")` marker either: a marker is recorded in
every new execution's history and can only be retired through
`deprecate_patch` plus a second deploy, so it belongs to the change that
actually ships the handoff, not to a branch that does not exist.

**Known simplification.** The delegated-claim path (a shepherd fixing a
steward-owned PR under the steward's epoch) is not fully wired: this repo has
no existing mechanism to read the CURRENT ownership epoch from a Tier 3 CLI
process, so both `run_implementer` and `run_shepherd` acquire claims with
`owner_epoch=0` rather than the steward's live epoch. Epoch fencing is
therefore inert for CLI-originated claims until that read is added; the
`entity_version` fence (the git head SHA, via `--force-with-lease`) is
unaffected and remains the authoritative CAS for every push in this
repository. Merge authority is unaffected either way: it is re-evaluated at
the merge boundary through `policy.merge_authority_for`,
`run_shepherd._service_mode` and `NEVER_MERGE_SERVICES`, none of which read a
claim.

## Testable invariants

1. Two concurrent acquires on one `(entity, phase)` produce exactly one winner,
   and every loser names the same winner. Proven in Go against real Postgres —
   a Python test with mocked HTTP can only show the client *handles* a 409, not
   that the database *produces* one. mctl-api CI already provides this: the
   `test` job in `.github/workflows/validate.yml` runs a `postgres:16` service
   and sets `TEST_DATABASE_URL`, so the store tests that skip on a developer
   laptop do run on every PR. It runs `go test -p 1 ./...` deliberately:
   *without* it, packages sharing that one database would wipe each other's
   rows, because several of them clean up with unscoped deletes. The lifecycle
   store's fixtures therefore scope their cleanup to their own keys, so that
   serialisation is not the only thing standing between them and the same
   problem from a new direction.
2. A pre-handoff executor cannot mutate after the epoch increments.
3. A claim pinned to head A cannot mutate once head B is current — enforced by
   the target system's own precondition (`--force-with-lease`,
   `--match-head-commit`), not only by the epoch check, so a worker that stalls
   between checking and pushing still fails.
4. Retry, replay and pod restart do not duplicate an effective mutation.
5. No lifecycle HTTP call originates in workflow code.
6. The `.status.yaml` projection can be made to contradict Postgres without
   changing any decision.
7. A principal cannot release or progress a row it does not own.
8. Each of the four rollout modes behaves per the table — including the
   `enforce` case where old says free, new says owned, and nothing mutates.
9. Ownership survives worker and pod death, and does not depend on a heartbeat.
10. A takeover is licensed by **liveness alone** — an owner unseen past its
    liveness bound — and never by lack of progress. An owner that is alive but
    has effected nothing is `stuck`: it is escalated to a human and keeps the
    entity. The earlier single-bound model said the opposite ("staleness
    requires progress evidence"), which is what made a PR waiting on human
    review indistinguishable from a crashed worker.
11. One fully missed tick does not make a live owner look dead; the liveness
    bound exceeds two cadences for exactly that reason.

Every guard is proved by mutation in both directions. A guard that can only pass
is not a guard.
