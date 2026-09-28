# ADR 019 — Context strategy release contract: version, promote, observe, roll back

> **Status:** proposed
> **Date:** 2026-09-27
> **Issue:** mctlhq/mctl-agents#472 (parent: ADR 009 amendment 1's closing
> line, `docs/adr/009-context-snapshot-contract.md`: "Wiring
> promotion/rollback of strategies is mctlhq/mctl-agents#472; measuring them
> is #266"; ADR 015 sec. "Non-goals" repeats it: "promoting or rolling back a
> strategy on this evidence... is #472")
> **Supersedes:** nothing. It defines the release lifecycle around a context
> strategy; it does not reopen `ContextSnapshot`'s schema (ADR 009 sec. 1-7)
> or the evaluator contract (ADR 015).
> **Delivery:** split into three slices, each its own review/approval. This
> ADR fixes the whole contract; Slice A shipped the catalog and loader.
> Slice B (mctlhq/mctl-agents#527) wired the rollout ladder and the
> `observe` shadow pass. Slice C (mctlhq/mctl-agents#528) has now shipped
> `orchestrator/context_release.assess_production_evidence`, gating a
> production promotion on mctlhq/mctl-agents#526's evaluator evidence — a
> production promotion is possible for the first time (sec. 2's refusal
> precedence table and README.md's "### Context strategy release" runbook).
> **No production promotion may treat `evidence.kind: none` as sufficient**;
> that remains true, but it is no longer the ONLY thing this ADR enforces
> outright — the real checks (exact identity, <= 7 days, >= 3 consecutive
> `observe-candidate` observations, no `hash-mismatch`) now run.

## Context

`orchestrator/context_assembly.py` already assembles, ranks, deduplicates,
budgets and seals a `ContextSnapshot` for every issue investigation (ADR
009, mctlhq/mctl-agents#265/#471), and every snapshot records which
strategy produced it in its `ContextStrategy` block
(`context_snapshot.py:649`). What the platform has no contract for is the
*other* direction: how a strategy becomes the one that runs. Today that
answer is a single environment variable,
`ISSUE_INVESTIGATOR_CONTEXT_STRATEGY`, read in `AssemblyConfig.from_env()`
and validated against a two-entry tuple of module constants (`STRATEGIES`,
`context_assembly.py:84`). The version a snapshot claims —
`STRATEGY_VERSION = "1.0.0"` — is a string literal that names no bytes:
`rank_candidates` can be edited and every snapshot will still claim
`trust-freshness-ranked 1.0.0`. There is no per-environment binding, no
revision history, no recorded promoter, no evidence link, and no rollback
primitive beyond unsetting the env var.

This ADR mirrors two precedents already in this repository rather than
inventing a third vocabulary:

- **ADR 007** (`docs/adr/007-agent-definition-execution-profile-contract.md`)
  already runs exactly this lifecycle for agents: immutable published
  versions, one atomic per-environment binding, and a
  `publish -> promote -> deprecate -> disable -> rollback` transition table.
  `orchestrator/resolver.load_release_binding` (`resolver.py:768-862`) is
  the concrete loader this ADR's binding loader follows: apiVersion/kind
  allow-list, a path-vs-`metadata` cross-check, a required content pin
  (`"a pin that can describe different content than the plan was built from
  is not a weaker guarantee, it is the absence of one"`), and a non-`published`
  lifecycle refusal.
- **This repository's own four-stage rollout ladders**,
  `orchestrator/lifecycle/rollout.py` and `orchestrator/work_context/rollout.py`,
  share one vocabulary (`off/observe/enforce/only`), one `at_least()`
  helper, and one rule: an unrecognised value answers `off` and warns,
  never raises.

## Decision

### 1. `ContextStrategyVersion` — an immutable, content-pinned version

Committed at `config/context-strategies/versions/<name>/<version>.yaml`:

```yaml
apiVersion: context.mctl.ai/v1alpha1
kind: ContextStrategyVersion
metadata:
  name: trust-freshness-ranked
  version: 1.0.0
spec:
  lifecycle: published            # published | deprecated | disabled
  ranker:
    name: trust-freshness-recency
    version: 1.0.0
  implementation:
    files:                        # hashed, sorted, length-prefixed
      - orchestrator/context_assembly.py
      - orchestrator/context_snapshot.py
    implementationHash: "sha256:..."
  contentHash: "sha256:..."       # over this document minus contentHash
  agents: [issue-investigator]
```

`name` must be one of the strategies `orchestrator/context_assembly.py`
actually implements (`STRATEGIES`); `version` must be semver. An unsupported
`apiVersion`/`kind` is rejected loudly, never silently defaulted — mirroring
`orchestrator/manifest.py`'s `SUPPORTED_API_VERSIONS` allow-list.

`implementationHash` is the load-bearing field: it is what makes `1.0.0`
name bytes instead of a string nobody checks. It is computed as a sha256
over the declared implementation files, in sorted order by path, as
length-prefixed `"<len>\n<path><len>\n<bytes>"` pairs — the identical
unambiguous encoding `tools/publish_agent_release.py`'s `prompt_hash`
already defines, for the identical reason: plain concatenation lets text
move between one file's path and the next file's content without changing
the digest. `contentHash` is a sha256 over the document's own canonical
JSON minus `contentHash` itself, the same self-describing pin
`resolver._model_policy_version()` uses for `config/model-policy.yaml`.

A version document is immutable: a behaviour change is a new version
document, never an edit to a published one. A recomputed
`implementationHash` that disagrees with the committed value fails the
load closed, naming the exact republish command
(`python tools/context_release.py publish --strategy <name> --version <version>`);
that version does not resolve. `spec.lifecycle` reuses ADR 007's
`published`/`deprecated`/`disabled` vocabulary and its rules verbatim:
`deprecated` still resolves for an existing binding but refuses a new
promotion; `disabled` refuses to resolve at all.

### 2. `ContextStrategyBinding` — atomic, append-only, per (agent, environment)

Committed at `config/context-strategies/bindings/<environment>/<agent>.yaml`,
laid out like `CATALOG_RELEASES_DIR / environment / f"{agent}.yaml"` so the
path/metadata cross-check is the same check `resolver.load_release_binding`
performs:

```yaml
apiVersion: context.mctl.ai/v1alpha1
kind: ContextStrategyBinding
metadata:
  agent: issue-investigator
  environment: shadow
spec:
  history:
    - revision: 1
      strategy: deterministic-fixed-order
      version: 1.0.0
      contentHash: "sha256:..."
      implementationHash: "sha256:..."
      promotedBy: "<github-login>"
      promotedAt: "2026-09-27T00:00:00Z"
      reason: "inert shadow baseline: today's default, made explicit"
      evidence: {kind: none, ref: null, evaluatorVersion: null}
```

`metadata.environment`/`metadata.agent` must agree with the path the
document was read from. `history` is append-only; the entry with the
highest `revision` is active. Revisions are positive integers, strictly
increasing, with no gap and no reuse. A promotion appends one new revision
carrying the strategy's name, version, `contentHash`, `implementationHash`,
promoter, timestamp, reason and an `evidence` block; it never mutates or
deletes a prior revision. A rollback appends a revision that copies an
exact prior revision's `(strategy, version, contentHash, implementationHash)`
tuple and records `rollbackOf: <revision>` — never infers "one step back".
A rollback naming a revision whose version has since become `disabled` is
refused, naming the disabled version.

**Evidence.** `evidence.kind: none` is legal only for `shadow` — shadow is
not an authoritative production selection, so a recorded `reason` is
sufficient. `production` requires `evidence.kind: context-eval`, naming the
exact strategy/version/`contentHash`/`implementationHash`, an evaluator
version, an observation timestamp (`evidence.observedAt`) and a
consecutive-observation count (`evidence.observations`) — the two fields
Slice C adds to `ContextStrategyBindingRevision`, both required (non-empty
string / positive integer) for a `context-eval` revision and both absent
for a `none` one; `load_binding()` fails closed with `unknown` naming
whichever is wrong. Evaluated evidence is refused as `evidence-mismatch`
unless its identity exactly matches the version being promoted,
`evidence-stale` when its newest observation is older than **7 days**, and
`evidence-insufficient` unless it represents at least **3 consecutive
observe-mode investigations** with no `hash-mismatch` verdict. These are
validation rules, not automatic promotion: a human-reviewed binding PR is
still required. The 7-day window and the 3-run minimum are **v1 promotion
policy constants** — release policy, not a property of mctlhq/
mctl-agents#526's evaluator — and change only by amending this ADR.

Production evidence comes ONLY from mctlhq/mctl-agents#526's non-
authoritative `observe-candidate` (Slice B's `observe` shadow pass's
candidate snapshot, evaluated in the same production investigation with its
own `execution_ref`, never a borrowed `store_ref` — the candidate is never
persisted). Making the candidate authoritative to collect evidence (a
binding at `enforce`/`only`, or `ISSUE_INVESTIGATOR_CONTEXT_STRATEGY`) is
production exposure before the gate and does not satisfy it: a `live`
record of an authoritative run is dropped before assessment, along with
every `stored-replay`/`fixture-baseline`/`none` record.

`orchestrator/context_release.assess_production_evidence` (Slice C, mctlhq/
mctl-agents#528) is the soak gate's normative definition, with this fixed
refusal precedence — never order-dependent, each a distinct
`ContextReleaseError.code`:

| # | Condition | Code |
|---|---|---|
| 1 | `promote()`'s own `evidence_kind != "context-eval"` (including `"none"`) | `evidence-missing` |
| 2 | No `evidence_kind: "observe-candidate"` records supplied | `evidence-missing` |
| 3 | A record for the promoted identity carries `verdict: "hash-mismatch"` | `hash-mismatch` |
| 4 | The supplied `evidence.evaluatorVersion` disagrees with the records' own `evaluator_version` | `evidence-mismatch` |
| 5 | `context_eval.assess_evidence` -> `missing` | `evidence-missing` |
| 6 | `context_eval.assess_evidence` -> `mismatched` | `evidence-mismatch` |
| 7 | `context_eval.assess_evidence` -> `stale` | `evidence-stale` |
| 8 | `context_eval.assess_evidence` -> `insufficient-observations` | `evidence-insufficient` |
| 9 | `context_eval.assess_evidence` -> `fresh` | accepted; the revision's `evidence` block records `kind: context-eval`, `ref`, `evaluatorVersion`, the newest counted `observedAt` and the `observations` count |

Step 3 runs before `assess_evidence` (steps 5-9) on purpose:
`assess_evidence` silently drops a non-`evaluated`-verdict record from
`usable`, so without step 3 a hash-mismatched soak would surface as
`evidence-insufficient` and hide the real fault.

`mctlhq/mctl-agents#526` was the undelivered evaluator half of #266; Slice C
has now landed on top of it. The release layer consumes #526's
closed-vocabulary verdicts and correlation fields rather than reimplementing
evaluation metrics: `#472` owns release decisions, `#526` owns evaluation.
`assess_production_evidence` imports `orchestrator.context_eval` inside its
own function body only, never at module scope, and reads no environment
variable and no clock of its own (`now` is caller-supplied, exactly as
`context_eval.assess_evidence` requires) — both invariants hold a source
scan in `tests/test_context_release.py`.

### 3. `orchestrator/context_release.py` — loader, resolver, promote/rollback

One new module, parsing YAML like every other catalog in this repository
(`resolver._read_yaml_and_hash`'s discipline: parse and hash the same
bytes). It is **not** stdlib-only like `context_snapshot.py`/
`context_assembly.py`, and it is imported by neither of those two at module
scope — `context_assembly.py` stays stdlib-only on purpose
(`context_assembly.py:22-27`), and Slice B's rollout wiring imports this
module the same deferred, inside-the-function way
`context_assembly._work_context_active`/`_client`/`_persist_to_work_item_store`
already import `orchestrator.work_context`.

Public surface: `load_version`, `load_binding`, `load_binding_or_none`,
`resolve(agent, environment) -> ResolvedContextStrategy`, and the pure
document builders `promote()`/`rollback()`, which never write — the CLI
writes. Every failure raises `ContextReleaseError`, whose `.code` is drawn
from a closed vocabulary (`VERDICTS`), in `orchestrator/lifecycle/contract.py`'s
style: anything this module cannot classify is `unknown`, never a silent
`ok`.

### 4. Rollout ladder and resolution (Slice B)

| `CONTEXT_RELEASE_ROLLOUT_MODE` | Behaviour |
|---|---|
| `off` (default) | No binding is loaded. `AssemblyConfig.from_env()` decides exactly as it does today — byte-for-byte. |
| `observe` | The binding is resolved and logged. The bound strategy runs as a **second, non-authoritative** `run_pipeline` pass over the same candidate list; the env var still decides what the model reads. Neither the pass nor its snapshot is persisted, rendered, or returned. |
| `enforce` | The resolved binding selects the authoritative strategy. An unresolvable binding blocks when `CONTEXT_RELEASE_REQUIRED` (default true); when false, falls back to `deterministic-fixed-order`, logging `binding-unresolved-fallback-default`. |
| `only` | As `enforce`, and a set `ISSUE_INVESTIGATOR_CONTEXT_STRATEGY` is a hard error — the env var can never silently shadow the binding. |

An unrecognised mode value answers `off` and warns, never raises — copied
from `work_context/rollout.py:mode()`. This ladder and its wiring into
`context_assembly.assemble()` are Slice B (mctlhq/mctl-agents#527); Slice A
ships the catalog and loader only, and nothing reads them at runtime yet.

### 5. Observability (Slice B, amended by Slice C)

One structured `CONTEXT_STRATEGY_RELEASE` line per resolution (mode, agent,
environment, strategy name, the bound strategy name, version, content hash,
binding revision, `override_active`, verdict) and, at `observe`, one
`CONTEXT_STRATEGY_COMPARE` line carrying both strategies' identity, both
`snapshot_id`s and — added by Slice C, mctlhq/mctl-agents#528 — the
evaluator reference an operator needs to correlate this line with the
`[context] context_eval=` records it joins on `snapshot_id`: `record_kind`,
`evaluator_name`, `evaluator_version` and `metrics_contract_version`, read
through a deferred `orchestrator.context_eval` import that yields four
`null`s on an `ImportError` rather than failing an already-completed run.
Every line carries ids, kinds, closed-vocabulary codes, versions, hashes,
counts and ratios only — never a `locator`, a `selector`, or any byte
derived from a retrieved payload (ADR 009 sec. 5, ADR 015 sec. 3). The
compare line still carries no counter delta, no ratio and no verdict about
which strategy performed better — that stays mctlhq/mctl-agents#526's
evaluator's own job, never re-derived here against a shadow snapshot that is
deliberately never persisted.

### 6. Durable provenance — ADR 009 amendment 2

`ContextStrategy` gains two optional fields, `release_revision: int | None`
and `content_hash: str | None`, following ADR 009 amendment 1's `conflicts`
precedent byte-for-byte: `to_dict()` omits each when unset, they enter the
snapshot's own `content_hash` only when present, and every snapshot sealed
at mode `off` — including the golden fixture — keeps its exact bytes and
`snapshot_id`. See ADR 009's amendment 2 for the field table; this ADR
defines what the two values mean (a `ContextStrategyBinding` revision and a
`ContextStrategyVersion`'s `contentHash`), ADR 009 defines how they enter
the snapshot.

### 7. `tools/context_release.py` — the operator CLI

Mirrors `tools/publish_agent_release.py`'s shape (argparse, `--dry-run`,
prints what it did, per-target isolation): `publish` writes/refreshes a
version document with freshly computed hashes; `promote` appends a
revision; `rollback` appends a restoring revision; `resolve` prints what a
run would resolve today, including every check that passed — this is also
the CI preflight. Because the catalog is committed in this repository,
every one of these is a reviewed commit: the promoter identity is the
commit author and the audit trail is git history, matching ADR 007's "Git/
GitOps owns reviewed draft definitions and desired catalog state" while
"what actually ran" stays with the snapshot and the execution record.

### 8. CI guard

A pytest test recomputes every published version's `implementationHash`
against the working tree and fails with the exact republish command when a
change to `orchestrator/context_assembly.py` or `context_snapshot.py` did
not come with a republished version. Without this, version identity can
silently drift from the implementation it names — the exact gap sec. 1
exists to close.

### Source-of-truth boundary

| Layer | Owns | Must not answer |
|---|---|---|
| Git (this repository) | Reviewed, immutable strategy versions and atomic per-environment bindings; the promotion/rollback history. | Whether a workflow is currently running. |
| Runtime `ContextSnapshot` | The exact strategy/version/hashes/binding revision a specific execution actually sealed under. | Current desired state or later promotions. |

### Safety invariant

A strategy version, a binding revision and a comparison metric are
ordering and measurement only. No promotion, binding or score produced by
this contract may ever be read by a policy, capability-eligibility or
authorization decision, and no module this ADR adds may be imported by a
policy path — restating ADR 009 sec. 5's non-negotiable sentence
("context relevance is never an authorization mechanism") and extending its
existing field-name and import-direction tests
(`tests/test_context_snapshot.py`) to cover `orchestrator/context_release.py`.

## What this ADR may not be reopened to change

Mirroring ADR 007/009's own discipline: a follow-up may add
producer/telemetry/CLI-ergonomics detail, but must not reopen —

- the version document's identity fields (`apiVersion`, `kind`, `name`,
  `version`, `contentHash`, `implementationHash`) or the length-prefixed
  hashing encoding;
- the `published`/`deprecated`/`disabled` lifecycle and its resolution
  rules;
- the append-only, gapless, strictly-increasing revision history rule, or
  "rollback restores an exact revision, never a guess at one step back";
- the safety invariant (context relevance/version/binding is never an
  authorization input);
- the `off/observe/enforce/only` vocabulary or the "unrecognised value
  answers `off` and warns" rule;
- production's evidence requirement — `evidence.kind: none` may never
  become sufficient for a production promotion; only amending this ADR may
  change the 7-day/3-run v1 policy constants.

## Non-goals

- A new mctl-api table, route or registry client for context strategies.
  The catalog is committed in `mctl-agents` for v1; a cross-repo
  gitops/registry promotion path is a named follow-up once this in-repo
  catalog is proven.
- Implementing `orchestrator/context_eval.py`, its fixture set, baseline or
  replay CLI — that is mctlhq/mctl-agents#526. This ADR defines and
  validates the release-side evidence reference; it does not duplicate
  evaluator logic.
- Writing a new strategy or ranker, or changing what either existing
  strategy selects.
- Extending the lifecycle to agents other than `issue-investigator`.
- Per-tenant or per-repository strategy selection.
- Automatic promotion on a metric threshold. Every promotion is a reviewed
  commit by a human.

## Alternatives

1. **New mctl-api tables/routes mirroring the agent registry, promoted by
   an MCP call.** Rejected for v1: a binding able to move independently of
   the image could select a version whose implementation is not in the
   running worker — the exact floating-half failure
   `resolver.py:806-811`/ADR 007 sec. 4 describe for an unpinned
   `definition.version`. Named as the follow-up once the in-repo catalog is
   proven.
2. **Keep the env var, give it a richer grammar
   (`STRATEGY=trust-freshness-ranked@1.1.0`).** Cheapest, and what this ADR
   replaces. Rejected: no revision history, no prior-revision rollback
   target, no promoter, no reason, no evidence link, and the version string
   still names no bytes.
3. **Fold the strategy into the existing `ExecutionProfile`/`ReleaseBinding`.**
   Rejected: ADR 007 fixes what a profile version bump means (model,
   skills, tools, permissions, budget, timeout, runtime, approval,
   evidence); adding context assembly would make one revision mean two
   unrelated things and force a full agent re-promotion for a ranking
   tweak.
4. **Derive the version entirely from the implementation hash — no human
   semver.** Rejected: unreadable in a changelog or incident review, and
   every whitespace edit becomes an automatic "promotion" with no human
   decision. The chosen design keeps semver as the human handle and the
   hash as the pin — `resolver._model_policy_version()`'s existing
   compromise.

## Platform impact

- **Migrations:** none. No mctl-api table, no route, no GitOps schema
  change, no agent manifest change.
- **Backward compatibility:** default `off` (Slice B) means the
  investigator's observable behaviour, prompt bytes and snapshot ids are
  unchanged until an operator moves one environment variable.
  `ContextStrategy`'s two new fields are optional and omitted-when-unset,
  so every persisted snapshot and the golden fixture keep their identity.
- **Resource impact:** at `observe` (Slice B), one extra `run_pipeline`
  pass plus one extra `seal()` per investigation — pure CPU over an
  in-memory list already bounded by `AssemblyConfig.max_candidates`/
  `max_bytes`. At `enforce`/`only`, one YAML read and two sha256
  computations per run, cached per process. Slice A adds no runtime cost at
  all: nothing reads the catalog outside tests and the CLI.
- **Risks and mitigations:**
  - *Implementation-hash churn.* Whole-file hashing means a comment edit in
    `context_assembly.py` fails the CI guard. Mitigated: the guard prints
    the exact republish command; over-reporting change is the fail-safe
    direction. Per-function hashing is a follow-up.
  - *A binding names a version the running image does not implement.*
    Mitigated by resolving `implementationHash` against the files in the
    image and failing closed, plus `tools/context_release.py resolve` as a
    CI preflight on the same commit.
  - *Promotion without valid evidence.* Production accepts only #526
    `context-eval` evidence matching exactly, <= 7 days old, and covering
    >= 3 consecutive `observe-candidate` observations with no
    `hash-mismatch` (sec. 2's refusal precedence table, shipped by Slice C,
    mctlhq/mctl-agents#528). Missing/stale/mismatched evidence fails closed;
    promotion is still a reviewed commit, never an automatic metric action.
  - *A binding or score drifts onto an authorization path.* Mitigated by
    the safety invariant above and its tests.
- **Security:** no new secret, no new network call, no new credential. The
  catalog is non-sensitive configuration; log lines carry ids, versions,
  hashes and counts only.

## Implementation map (Slice A)

This ADR ships with Slice A only:

```
docs/adr/019-context-strategy-release-contract.md              # this ADR
docs/adr/009-context-snapshot-contract.md                       # amendment 2, follow-up table row, sec. 1's link
docs/adr/015-context-evaluation-contract.md                     # non-goal line now links here
orchestrator/context_snapshot.py                                # ContextStrategy gains two optional fields
orchestrator/context_release.py                                 # new module: loader/resolver/promote/rollback
tools/context_release.py                                        # new operator CLI
config/context-strategies/versions/deterministic-fixed-order/1.0.0.yaml
config/context-strategies/versions/trust-freshness-ranked/1.0.0.yaml
config/context-strategies/bindings/shadow/issue-investigator.yaml
tests/test_context_snapshot.py                                  # amendment 2 tests, T11 extension
tests/test_context_release.py                                   # new tests
```

Nothing in `orchestrator/context_assembly.py` changes, and no production
binding is written. Slice B (mctlhq/mctl-agents#527) wires the rollout
ladder. Slice C (mctlhq/mctl-agents#528) has now shipped the production
evidence gate (`assess_production_evidence`), the `execution-observed`
provenance mode (ADR 015 sec. 1), the compare line's evaluator reference
(sec. 5), and the operator runbook (README.md's "### Context strategy
release"); it ships no `production` binding for `issue-investigator`
either — that stays a separate, reviewed PR by an operator once real soak
evidence exists.
