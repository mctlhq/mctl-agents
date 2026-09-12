# ADR 009 — `EntityRef`, `LifecycleOwnership` and `ExecutionClaim` contract

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
| a second **scheduler** (epic invariant 9) | no timer, no queue, no `due_at`, no `next_action`, no background sweep. It never calls GitHub, never submits Argo, never signals Temporal. `stale` is **derived on read** by the caller's existing tick (§4), never materialised — materialising it is precisely what would make it a scheduler. |
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
| atomic acquire, loser learns the winner | `INSERT … ON CONFLICT DO NOTHING RETURNING` |
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

| kind | phase | replaces | staleness bound |
|---|---|---|---|
| `devloop-proposal` | `implement` | the 130-minute `attempt` lease | 130 min |
| `pull-request` | `review-remediation` | `_dev_loop_owns` + skip-lists + `merge_owner` | 6 h |

Six hours is four times the in-loop tick cadence (`SHEPHERD_TICK_EVERY_POLLS = 8`
at a 30-minute poll ≈ 4 h), so a single missed tick cannot make a healthy owner
look stale.

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
  acquired_at, last_progress_at, progress_evidence
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

**`stale` and `conflicted` are not stored.** Both are derived on read:

```text
stale      := state = 'active' AND now() - last_progress_at > bound(kind, phase)
conflicted := an active claim whose owner_epoch <> the current epoch,
              or whose entity_version <> the current head
```

A second active owner is not in that list because it is impossible: the partial
unique index makes "at most one active owner" a property of the database rather
than of the code that writes to it.

**Progress means an effected state change, not a heartbeat.** A poll that
observes nothing new writes no progress, and `progress_evidence` records what
counted. #350 requires that a heartbeat alone must not prove useful progress
indefinitely; a liveness ping that refreshed `last_progress_at` would do exactly
that.

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
`acquire(force_if_stale=true, expected_epoch=N, evidence=…)` re-checks staleness
*server-side* under the advisory lock, bumps the epoch, and writes a `recovered`
event. A client may not assert staleness; it may only ask the server to
re-evaluate it.

### 6. The fencing rule, and where it is enforced

> Every mutating lifecycle action carries
> `(entity_kind, entity_id, phase, owner_epoch, claim_id, entity_version)`. The
> server accepts it only if the ownership row still has that `epoch`, the claim
> is `active` and unexpired, and the claim's `entity_version` equals the current
> head. Otherwise it returns **409 FENCED** and the worker **aborts without
> mutating**.

The enforcement *point* matters more than the rule. A validity check far from
the mutation is decoration, so re-validation happens at exactly two boundaries
and nowhere else:

- immediately before the `git push` in `apply_followup`;
- immediately before `gh pr merge` in `merge_pr`.

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

**2 — direct implementer PR (#239).** On PR creation the implementer
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
  to one input to owner liveness, which §5's recovery rule needs: staleness must
  be judged on progress evidence and health, not on whether a process name
  exists.
- **`merge_owner` is retained, still unread, and documented as descriptive**
  (#344 item 1). The canonical answer is `merge_authority_for`.

### 13. Metrics

Shipped with the store rather than retrofitted: `owner_acquire`,
`ownership_conflict`, `store_unknown`, `divergence`, `claim_fenced`, `handoff`,
`stale_owner`, `orphan_adopted`. The soak gate is read from `store_unknown` and
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
| 2 | #352 | `ExecutionClaim`, database-clock lease and renew, epoch fencing, handoff |
| 3 | #353 | reconciler over known ownership rows; adoption integration after #334 |
| 4 | mctl-api#294 | guarded operator recovery with optimistic preconditions |
| 5 | — | extension beyond DevLoop, gated on the pilot; hand-off to `mctlhq/.github#21` and `#42` |

Phase 1 acquires ownership only. No `ExecutionClaim`, no lease and no fencing
ship in #351; the existing 130-minute `attempt` lease is untouched until #352.

## Testable invariants

1. Two concurrent acquires on one `(entity, phase)` produce exactly one winner,
   and every loser names the same winner. Proven in Go against real Postgres —
   a Python test with mocked HTTP can only show the client *handles* a 409, not
   that the database *produces* one. mctl-api CI already provides this: the
   `test` job in `.github/workflows/validate.yml` runs a `postgres:16` service
   and sets `TEST_DATABASE_URL`, so the store tests that skip on a developer
   laptop do run on every PR. It runs `go test -p 1 ./...` deliberately, because
   packages sharing that database wipe each other's rows in parallel — so the
   lifecycle store's fixtures must clean up scoped to their own keys rather than
   issuing unscoped deletes.
2. A pre-handoff executor cannot mutate after the epoch increments.
3. A claim pinned to head A cannot mutate once head B is current.
4. Retry, replay and pod restart do not duplicate an effective mutation.
5. No lifecycle HTTP call originates in workflow code.
6. The `.status.yaml` projection can be made to contradict Postgres without
   changing any decision.
7. A principal cannot release or progress a row it does not own.
8. Each of the four rollout modes behaves per the table — including the
   `enforce` case where old says free, new says owned, and nothing mutates.
9. Ownership survives worker and pod death, and does not depend on a heartbeat.
10. A stale owner is not replaced on a delayed heartbeat alone; staleness
    requires progress evidence.

Every guard is proved by mutation in both directions. A guard that can only pass
is not a guard.
