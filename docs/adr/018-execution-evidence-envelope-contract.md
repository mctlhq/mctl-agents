# ADR 018 — `ExecutionEvidence` envelope contract (Tier A: construction, redaction, sealing)

> **Status:** proposed
> **Date:** 2026-09-27
> **Issue:** mctlhq/mctl-agents#520 (parent #199; supersedes the design of #483)
> **Supersedes:** the gitops-persistence design attempted in
> mctlhq/mctl-agents#483 (`orchestrator/evidence_store.py`, `_evidence/`
> trees written into the public `mctl-gitops` repository, 3650-day
> retention) — that design never landed on `main` and must not be continued

## Context

mctl-agents already has five sealed, canonical governance contracts, each
owned by exactly one store:

| Record | Prefix / key | Owner in this clone |
| --- | --- | --- |
| Execution | `we_` | `orchestrator/work_context/snapshots.py:39` (`EXECUTION_ID_PREFIX`), ADR 011 (`docs/adr/011-execution-identity-contract.md`) |
| Context snapshot | `cs_` (store) / `cs-` (local seal) | `orchestrator/work_context/snapshots.py:42`, `orchestrator/context_snapshot.py:1245`, ADR 009 |
| Execution request | `xr_` | `orchestrator/work_context/execution_requests.py:32` |
| Model usage | `(session_id, result_uuid, model_key)` | `orchestrator/usage_ledger.py:27`, ADR 012 |
| Human approval | `aar_` | `orchestrator/action_approvals.py:47` |
| Policy decision | `action_digest` | `orchestrator/policy_checkpoint.py:165` (`Decision`), ADR 014 |

What is missing is the one document that says, for a single governed
execution, *which* of those canonical records applied — a tamper-evident
envelope that joins them by reference. `orchestrator/context_snapshot.py:728`
already declares the seam for it:

```python
@dataclass(frozen=True)
class EvidenceRef:
    """A pointer into the #199 evidence store — `{evidence_id, kind}` only.
    No payload field exists; `from_dict` rejects any other key."""
    evidence_id: str
    kind: str
```

documented as "a pointer into the #199 evidence store." The referent does
not exist yet.

mctlhq/mctl-agents#483 tried to build that referent as
`orchestrator/evidence_store.py`, writing durable `_evidence/` trees into the
public `mctl-gitops` repository with 3650-day retention. That architecture
is invalid: it puts governance evidence in a public repository, it
duplicates state that is already canonical in mctl-api, its 3650-day
retention is a data-protection commitment mctl-agents cannot make on
mctl-api's behalf, and it predates `we_`, `cs_`, `xr_` and the shipped usage
ledger — so its joins would have to be rewritten anyway. **#483's design is
superseded by this ADR and must not be continued or repaired.**

This issue supersedes it and scopes down to **Tier A only: the pure evidence
contract** — construction, validation, redaction and content-addressed
sealing, with no persistence layer of any kind. **Durable storage and the
retrieval API are Tier B, owned by mctl-api**, and are a separate, later
mctl-api issue. Tier A ships one new, inert, additive schema module, exactly
as ADR 009 first shipped `orchestrator/context_snapshot.py`.

## Decision

### 1. Canonical shape — `evidence.mctl.ai/v1alpha1`, `kind: ExecutionEvidence`

An `ExecutionEvidence` envelope is a frozen, JSON-primitives-only document,
modelled field-for-field on `orchestrator/context_snapshot.py`'s
`ContextSnapshot`: frozen dataclasses, a `sha256:`-prefixed content hash,
`seal()` as the only constructor that fills identity, `from_dict` that
rejects unknown keys, and bounded string lengths. Implemented in
`orchestrator/execution_evidence.py`.

**`ExecutionEvidence`**

| Field | Type | Owner | Meaning |
| --- | --- | --- | --- |
| `api_version` | str | this contract | always `evidence.mctl.ai/v1alpha1` |
| `kind` | str | this contract | always `ExecutionEvidence` |
| `evidence_id` | str | `seal()` | `"ev-" + content_hash[7:23]`, derived not random |
| `content_hash` | str | `seal()` | `"sha256:..."` over every other field except `created_at` |
| `created_at` | str (ISO-8601) | caller | sealing time; excluded from the hash |
| `execution` | `ExecutionJoin` | caller | the execution this evidence is for — required (at least one of `execution_id`/`runtime_execution_id`, Amendment 1) |
| `outcome` | `Outcome` | caller | the final outcome — required, exactly one |
| `policy_decisions` | `PolicyDecisionRef[]` | caller | every policy decision this execution's actions produced — required by default |
| `snapshot_refs` | `SnapshotRef[]` (optional) | caller | `ContextSnapshot`s this execution sealed or consulted; absent when empty |
| `execution_request` | `ExecutionRequestRef \| null` (optional) | caller | the `xr_` request that dispatched this execution, if any |
| `usage` | `UsageRef \| null` (optional) | caller | join keys into the usage ledger, if any |
| `approvals` | `ApprovalRef[]` (optional) | caller | `aar_` approvals this execution's actions relied on |
| `artifacts` | `ArtifactRef[]` (optional) | caller | generated artifacts this execution produced |
| `gaps` | `Gap[]` | `_safe()` + caller | every block known to be missing, and why |

**`ExecutionJoin`** — `execution_id` (`we_`-validated), `work_item_id`,
`trace_id`, `runtime_execution_id` (`ex-`-validated, Amendment 1: two typed
identities, never one overloaded field).

**`SnapshotRef`** — `snapshot_id` (`cs_` or `cs-`), `content_hash`
(`sha256:`). No source, selector, locator or body — `EvidenceRef` already
forbids a payload on the other side of this seam.

**`ExecutionRequestRef`** — `request_id` (`xr_`), `kind` from
`execution_requests.KINDS`, `state` from `execution_requests.STATES`.

**`UsageRef`** — `session_id`, `result_uuid` (optional), `model_key`,
`devloop_stage` (optional, from `usage_ledger.DEVLOOP_STAGES`). Join keys
only — no token counts, no cost figures, no ledger row content.

**`ApprovalRef`** — `approval_id` (`aar_`), `intent_hash`, `state`
(`pending`/`approved`/`denied`/`expired`/`consumed`).

**`PolicyDecisionRef`** — `action_digest`, `verdict` (from
`policy_checkpoint.VERDICTS`), `code`, `policy_version`, `rule_id`,
`approval_ref`. Never the action's arguments.

**`ArtifactRef`** — `name` (bounded, no `/`, `\`, `..` or leading `~`),
`kind`, `content_hash` (`sha256:`). An immutable ref, never the artifact's
bytes.

**`Outcome`** — `code` from the closed `OUTCOME_CODES` set (`succeeded`,
`failed`, `refused`, `abandoned`, `superseded`), `reason_code` (a
machine-readable slug, never prose).

**`Gap`** — `block` from the closed `BLOCK_NAMES` set, `code` from the
closed `GAP_CODES` set (`not_produced`, `store_unavailable`,
`not_applicable`, `redacted_out`, `undecided`), `required: bool`.

No block here carries a source payload, a free-text field, a path, or a
filesystem/URL fragment: there is no such field declared, and
`orchestrator/execution_evidence.py`'s `from_dict` rejects any key it does
not know about — a future caller cannot smuggle one in.

### 2. Identity and immutability

`content_hash = "sha256:" + sha256(canonical JSON of every field except
content_hash, evidence_id, created_at)`, using `hash_bytes`/`canonical_json`
imported from `orchestrator.context_snapshot` — the single hashing and
canonicalization rule this repository declares
(`context_snapshot.py:117-144`). `evidence_id = "ev-" + content_hash[7:23]`
(the 16 hex characters immediately after the `"sha256:"` prefix) — derived
from content, never random, mirroring `context_snapshot.seal()`'s `cs-` rule,
`execution_identity.seal()`'s `ex-` rule and `capability.seal()`'s `cap-`
rule. This module defines no second hashing or canonicalization convention.

`seal()` is the only constructor that fills `content_hash` and
`evidence_id`; there is no mutator. `created_at` is caller-supplied and
excluded from the hash, so sealing identical inputs twice at different
wall-clock times yields the same identity. A change to any referenced id or
hash produces a new envelope with a new `content_hash`, never an in-place
edit. Every optional block (`policy_decisions`, `snapshot_refs`,
`execution_request`, `usage`, `approvals`, `artifacts`) enters the hashed
payload only when non-empty/present — the same rule
`context_snapshot.py:1174-1207` states for `work_context` and `conflicts` —
so a future optional block added to this schema cannot re-identify an
envelope already sealed today, and `recompute_content_hash()` reproduces a
sealed envelope's hash without mutating it.

### 3. Redaction

Every block of the envelope — not a subset — passes through `_safe()`
before the canonical bytes are computed, so `content_hash` and
`evidence_id` certify the redacted document. `_safe()` walks the entire
assembled payload recursively and, for each leaf, drops — never masks — a
value that is not a declared, bounded scalar or that matches a credential
shape, following the documented rule at `orchestrator/tracing_sdk.py:132-138`
("dropped, never masked": *"a masked value is a present attribute that reads
like data, an absent one is honest"*). The credential-shape screen
(`_CREDENTIAL_VALUE`, the length cap) is extracted, behaviour-preserving,
into a new stdlib-only `orchestrator/redaction.py` that both
`orchestrator/tracing_sdk.py` and this module import — one screen, not two
copies that could drift.

Every drop becomes an explicit `Gap(block=..., code="redacted_out",
required=<whether that block is required>)`, so redaction is visible rather
than silent and feeds `completeness` (sec. 4) automatically. The gap
accounts for a drop the way `context_snapshot.Redaction`
(`context_snapshot.py:296-324`) already documents: rule ids and volume only,
never the matched text.

### 4. Completeness and gaps

`completeness` is a derived `@property`, never a field: `INCOMPLETE` iff
`any(g.required for g in evidence.gaps)`, else `COMPLETE`. It is not in
`__init__` and not accepted by `from_dict` — a document that tries to supply
`completeness` directly raises `ExecutionEvidenceError` for the unknown key.
This is structural, not conventional: a caller cannot construct a
`COMPLETE` envelope while evidence is missing.

`Requirements` is a small frozen dataclass the caller passes to `seal()`
declaring which optional blocks this execution was supposed to produce. The
default profile: the execution join, the outcome and at least one
policy-decision reference are required; snapshots, the execution request,
usage, approvals and artifacts are required-if-applicable, declared by the
caller rather than guessed by the module. `seal()` raises
`ExecutionEvidenceError` when a block `Requirements` marks required is both
absent and ungapped — an omission is a hard error, not a quietly `COMPLETE`
envelope.

### 5. Boundary rules — normative and testable

| Concern | Owner | What `ExecutionEvidence` may record | What it must never do |
| --- | --- | --- | --- |
| Execution identity (#196, ADR 011) | `orchestrator/work_context/` (`we_`), `orchestrator/execution_identity.py` (`ex-`) | `execution_id`, `work_item_id`, `trace_id`, `runtime_execution_id` (`ExecutionJoin`) — a reference only, two typed identities never merged into one (Amendment 1) | Copy any `ExecutionContext` field beyond the join keys; overload `execution_id`/`runtime_execution_id` into one column |
| Context (#264, ADR 009) | `orchestrator/context_snapshot.py` | `snapshot_id` + `content_hash` (`SnapshotRef`) only | Carry a source, selector, locator or body |
| Execution requests (mctl-api#368) | `orchestrator/work_context/execution_requests.py` | `request_id`, `kind`, `state` | Carry request provenance beyond those three fields |
| Model usage (#199 parent scope, ADR 012) | `orchestrator/usage_ledger.py` | `session_id`, `result_uuid`, `model_key`, `devloop_stage` — join keys only | Carry a token count, a cost figure, or any ledger row content |
| Human approval (mctl-api#366) | `orchestrator/action_approvals.py` | `approval_id`, `intent_hash`, `state` | Carry the approval's intent fields or requester identity |
| Policy decisions (#197, ADR 014) | `orchestrator/policy_checkpoint.py` | `action_digest`, `verdict`, `code`, `policy_version`, `rule_id`, `approval_ref` | Carry the action's arguments |
| Authorization | Policy checkpoints + provider-side enforcement | Nothing | No allow/deny/permit/grant/authorized field exists anywhere in the schema |
| Persistence and retrieval (Tier B) | mctl-api (a separate, later issue) | Nothing in this module | This module writes no file, opens no socket, and defines no store; `orchestrator/evidence_store.py` and any `_evidence/` tree remain forbidden |

**Evidence records what happened; it never grants anything.** A recursive
field-name test (`tests/test_execution_evidence.py`) asserts no
`allow`/`deny`/`permit`/`grant`/`authorized` token appears anywhere in the
serialized schema, mirroring ADR 009 sec. 5's identical rule for
`ContextSnapshot`.

## Alternatives

1. **Repair `orchestrator/evidence_store.py` and `_evidence/` from
   mctlhq/mctl-agents#483.** Dropped. The issue forbids it, and
   independently it is wrong: it writes governance evidence into a *public*
   repository, it creates a second copy of work-item, execution, snapshot
   and usage state that must then be reconciled, and its 3650-day retention
   is a data-protection commitment mctl-agents cannot make on mctl-api's
   behalf. It predates `we_`, `cs_`, `xr_` and the shipped usage ledger, so
   its joins would have to be rewritten anyway — there is nothing left to
   salvage.
2. **Extend `ContextSnapshot` with the evidence blocks instead of a new
   module.** Dropped. ADR 009 is explicit that "evidence has exactly one
   owner" and that snapshots carry `evidence_refs: {evidence_id, kind}`
   only. Beyond the contract violation, it is mechanically unsafe:
   `_content_payload` hashes the snapshot's fields, so adding blocks would
   re-identify every already-sealed `snapshot_id`.
3. **Define the envelope as a JSON Schema document validated by a generic
   validator.** Dropped. The repository has no schema-validation dependency
   and every peer contract is expressed as frozen dataclasses with explicit
   `from_dict` validation. A JSON Schema could express field types but not
   the two rules that matter here — `completeness` derived from gaps, and
   `_safe()` running before the hash — so the interesting half of the
   contract would end up in Python anyway, in two places.
4. **Specify and build the envelope in mctl-api first.** Dropped for Tier A.
   The producer of the evidence is mctl-agents: it is the process that holds
   the `we_`, the sealed snapshot, the `xr_`, the policy decisions and the
   outcome at the moment they are true. Defining the contract where it is
   produced, with no persistence, is what makes Tier B a straightforward
   mctl-api issue rather than a negotiation.
5. **Duplicate the credential regexes into the evidence module instead of
   extracting `orchestrator/redaction.py`.** Dropped. Two copies of a
   credential screen drift, and the copy that drifts is the one that stops
   catching a new token format — the "second, disagreeing convention" risk
   ADR 009 already names for hashing. The answer here is the same as there:
   one shared, stdlib-only module.

## Non-goals

- Creating `orchestrator/evidence_store.py`, or anything resembling it.
- Writing evidence (envelopes, summaries, pointer files, indexes) into
  `mctl-gitops` or any other git repository.
- Any retention policy, and specifically any 3650-day public retention.
- Any second store for work items, executions, snapshots, execution
  requests, approvals or usage. This proposal reads no store and writes no
  store.
- The evidence retrieval API, durable persistence, and any mctl-api route.
  That is Tier B and a separate mctl-api issue.
- Continuing, repairing or shepherding mctlhq/mctl-agents#483.
- Wiring the envelope into `run_issue_investigator.py`,
  `run_implementer.py`, `run_shepherd.py` or
  `temporal/workflows/dev_loop.py` as a mandatory step. The module ships
  inert and additive, exactly as `context_snapshot.py` first shipped (ADR
  009: "the only code that ships with it is a new, inert, additive schema
  module").
- Changing `EvidenceRef`, `ContextSnapshot` or any already-sealed hash.
- Implementing #195 traces, #196 execution identity, #197 policy
  checkpoints, #198 human approval, or #242 capability discovery. This ADR
  fixes the seam this contract joins by reference; it does not reimplement
  any of them.

## Platform impact

**Migrations.** None. No schema, no database, no gitops file, no ArgoCD
application, no Helm value. Nothing is persisted, so there is nothing to
migrate.

**Backward compatibility.** Fully additive.
`orchestrator/execution_evidence.py` has no importer on day one.
`context_snapshot.EvidenceRef` is untouched, so no existing `snapshot_id` or
`content_hash` changes. The one edit to existing code —
`orchestrator/tracing_sdk.py` importing its credential/value screen from
`orchestrator/redaction.py` — is a pure move; `tests/test_tracing.py`,
`tests/test_tracing_agents.py` and `tests/test_tracing_temporal.py` cover it
and pass unchanged.

**Resource impact.** Nil at rest. At seal time: one recursive redaction
walk plus one `json.dumps` and one `sha256` over a document of bounded size
(every field is length-capped and every collection count-capped), so
microseconds and no allocation worth measuring. No network call, no file
handle, no new dependency in `pyproject.toml`.

**Risks and mitigations.**

- *The module becomes a covert payload store.* The exact risk ADR 009 lists
  for snapshots. Mitigated structurally: no block has a free-text or body
  field, every leaf is a slug/id/hash with a length cap, `_safe()` rejects
  undeclared structure and credential-shaped values before hashing, and a
  test asserts that `ArtifactRef.name` rejects `/`, `\` and `..`.
- *A second hashing or undecided convention creeps in.* Mitigated by
  importing `hash_bytes`/`canonical_json` from `context_snapshot` and
  `UNDECIDED_CODES`/`VERDICTS` from `policy_checkpoint`, plus a test that
  fails if the evidence module defines its own `hashlib.` call, its own
  `json.dumps(` call, or a literal `UNDECIDED_CODES` member.
- *Prefix drift between the evidence module and the owning stores.*
  Mitigated by a test asserting equality with
  `work_context.snapshots.EXECUTION_ID_PREFIX`,
  `work_context.snapshots.SNAPSHOT_ID_PREFIX`,
  `work_context.execution_requests.REQUEST_ID_PREFIX` and
  `action_approvals.ID_PREFIX`, so a rename in an owning module breaks this
  test rather than silently diverging here.
- *An envelope is sealed `COMPLETE` while evidence is missing.* Mitigated by
  `completeness` being a derived property (not constructible), by `seal()`
  raising when a required block is absent and ungapped, and by `_safe()`
  emitting a `redacted_out` gap for every value it drops.
- *Tier B later disagrees with this schema.* Mitigated by the `v1alpha1`
  `api_version` and `SUPPORTED_API_VERSIONS` map, the repository's
  established way to evolve a contract, and by this ADR recording the
  boundary explicitly.
- *Someone continues mctlhq/mctl-agents#483 anyway.* Mitigated by this ADR
  naming it superseded and by the module docstring restating the same
  non-goals, which is where the next implementer will actually look.

**Security.** The envelope is a strictly non-authoritative, payload-free,
redacted description. Provider-side authorization (GitHub, mctl MCP,
Kubernetes, the policy checkpoint) remains the only enforcement, unchanged
by this ADR.

## Implementation map

This PR changes only:

```
docs/adr/018-execution-evidence-envelope-contract.md
orchestrator/redaction.py                       # new, extracted from tracing_sdk.py
orchestrator/tracing_sdk.py                     # imports from redaction.py; no behaviour change
orchestrator/execution_evidence.py              # new module
tests/test_execution_evidence.py                # new tests
tests/fixtures/evidence/investigator-evidence.json  # new fixture
```

The contract is normative for Tier B and for every later wiring follow-up.
A follow-up may add a producer, persistence, or trace-emission
implementation detail, but must not reopen: the field shape and owner table
(sec. 1); the `content_hash`/`evidence_id` derivation (sec. 2); the
redaction-before-hash rule (sec. 3); the derived-`completeness` and
required-block-gap rules (sec. 4); or the boundary table, including that
persistence and retrieval are Tier B owned by mctl-api and that
mctlhq/mctl-agents#483's gitops-persistence design stays superseded (sec. 5).

**Amendment 1** (mctlhq/mctl-agents#539) additionally changed only:

```
docs/adr/018-execution-evidence-envelope-contract.md    # this amendment; sec. 1/5 edited in place
orchestrator/execution_identity.py                      # CONTEXT_ID_PREFIX extracted, literal-to-constant only
orchestrator/execution_evidence.py                      # ExecutionJoin gains runtime_execution_id
tests/test_execution_evidence.py                        # T15 + extensions to T3, T11, T13
tests/test_execution_identity.py                        # CONTEXT_ID_PREFIX drift assertion
tests/fixtures/evidence/implementer-evidence.json       # new fixture (ex- only)
tests/fixtures/evidence/shepherd-evidence.json          # new fixture (both identities)
```

`investigator-evidence.json` is unchanged, byte-for-byte.

## Amendment 1 — the execution join: `we_` and `ex-` as two typed fields (mctlhq/mctl-agents#539)

> **Status:** accepted

### Model (B): two typed fields, not a canonical resolution

Only `run_issue_investigator.py` ever attaches a `we_` — mctl-api mints one
per `(work_item_id, engine, engine_ref)` triple
(`orchestrator/work_context/executions.py:4-15`), reached through
`resolve_identity`/`attach_execution`. Every governed *mutation* — every
`ActionRequest`/`Decision` a policy checkpoint produces
(`policy_checkpoint.py:641`, `:678-682`), every `aar_` approval `intent_hash`
is bound to (`action_approvals.py:98-148`, `:416-419`) — carries only the
ADR 011 `ExecutionContext.context_id` (`ex-`), never a `we_`. Before this
amendment, `ExecutionJoin.execution_id` demanded `we_` unconditionally, so no
valid envelope could ever be sealed for an implementer run, a shepherd run
(including the #519/#524 gated merge), a policy decision or an approval —
exactly the executions #199 most needs to cover.

Two models were considered:

- **Model (A) — resolve every governed run to a canonical `we_`.** Rejected.
  It requires mctl-agents to either start minting `we_` locally (a second
  execution authority, which the issue forbids) or make every governed
  mutation depend on a network round trip to mctl-api and a work item that
  may not exist (an incident-responder sweep, a reconcile pass, a local
  run). It also does not remove the need for the `ex-` field: policy
  decisions and `aar_` intents are hash-bound to the runtime id regardless,
  so dropping it would break the deterministic linkage this amendment
  requires.
- **Model (B) — two distinct, typed fields, no overloading in either
  direction.** Chosen. `execution_id` stays `we_`-only; a new
  `runtime_execution_id` is `ex-`-only. Neither is derived from, copied
  from, or reconciled with the other — the producer supplies whichever it
  has, and the contract validates shape only. Model (A) remains available
  later as a purely additive *producer* enhancement: attaching a `we_` to an
  implementer/shepherd run costs nothing on this contract, the envelope
  simply carries both fields (the `shepherd-evidence.json` fixture below
  pins that exact shape).

This is exactly the defect `orchestrator/usage_ledger.py:59-66` and
`model_usage_records.execution_id` already carry: one untagged field, two
identifier shapes, indistinguishable to any consumer except by regex
guessing. This amendment refuses to add a second instance of it.

### The four-field `ExecutionJoin`

| Field | Type | Validation | Meaning |
| --- | --- | --- | --- |
| `execution_id` | str, optional | must start with `we_` (`EXECUTION_ID_PREFIX`); must NOT start with `ex-` | the #196 work execution mctl-api mints, when one was attached |
| `work_item_id` | str, optional | unvalidated (unchanged) | the work item `execution_id` was attached to |
| `trace_id` | str, optional | unvalidated (unchanged; a tightening to ADR 011's 32-lowercase-hex shape is a named follow-up, not made here — see requirements.md open question 3) | a correlation id, historically a Temporal workflow id |
| `runtime_execution_id` | str, optional | must fullmatch `ex-[0-9a-f]{16}` (`RUNTIME_EXECUTION_ID_PREFIX` + the exact shape `execution_identity.seal()` derives); must NOT start with `we_` | the ADR 011 `ExecutionContext.context_id` every governed mutation is stamped with |

Both identity fields are now optional at the dataclass level (`seal()`, not
`__init__`, enforces requiredness — sec. 4 below). Validation is symmetric
and cross-rejecting: an `ex-` value in `execution_id` and a `we_` value in
`runtime_execution_id` both raise `ExecutionEvidenceError`, naming the field
the value belongs in, so the two namespaces can never silently merge.

### Primary retrieval identity — derived, never stored, never the only lookup key

`evidence_id` remains the envelope's own content-derived key (sec. 2,
unchanged). For *execution-scoped* retrieval — "find the evidence for this
execution" — `ExecutionJoin` gains a derived, typed property:

```python
@property
def primary_execution_ref(self) -> tuple[str, str]:
    """(kind, id), kind in EXECUTION_REF_KINDS = {"work", "runtime"}."""
```

`work` wins when both are present; `("runtime", runtime_execution_id)` when
only that is set; `("", "")` when neither is. It is a `@property`, never a
field: not in `__init__`, not accepted by `from_dict`, never entering
`_content_payload` or the hash. `to_log_dict()` gains
`primary_execution_kind` (the kind only, per ADR 018's "counts and codes,
never ids or lists" trace surface) — the id is deliberately not exported to
telemetry.

**Tier B (mctl-api#409) must store and index `execution_id` and
`runtime_execution_id` as two separate typed columns, each independently
queryable — never one untagged column, and never indexed by
`primary_execution_ref` alone.** A both-identities envelope's primary is its
`we_`, but it must still be reachable by its `ex-`: that is precisely how
evidence is reached from a `POLICY_DECISION` or an `aar_` approval, which
carry only the runtime id. Indexing only the primary pair would make those
envelopes unreachable by the very identifier the deterministic-linkage rule
below binds them to.

### Deterministic linkage

Every `POLICY_DECISION` a policy checkpoint produces carries
`ctx.context_id` as its `execution_id` (`policy_checkpoint.py:641`,
`decision_record()` at `:551-552`); every `aar_` approval's `intent_hash` is
bound to that same runtime id via `ActionIntent.execution_id`
(`action_approvals.py:140-148`, redemption check at `:416-419`). This
amendment states the rule those two facts imply: **a `PolicyDecisionRef` or
`ApprovalRef` inside an `ExecutionEvidence` envelope is reachable from its
originating `POLICY_DECISION`/`aar_` record only through the envelope's
`execution.runtime_execution_id`**, never through `execution_id`. A work
execution (`we_`) is the linkage a `SnapshotRef` or an `ExecutionRequestRef`
resolves through instead. No cryptographic proof ties a `we_` and an `ex-`
inside the same envelope to the same real-world run — that linkage lives in
the work-item layer that attaches both, not in this payload-free contract;
the producer owns pair consistency, this contract validates shape only.

### Completeness for a runtime-only run

`seal()`'s required-block check for `execution` becomes: present iff
`execution_id` **or** `runtime_execution_id` is non-blank. An implementer or
shepherd run carrying only an `ex-`, an outcome and at least one policy
decision now seals `COMPLETE` with no gap — the `implementer-evidence.json`
fixture below is exactly that shape. An envelope with neither identity still
raises `ExecutionEvidenceError` unless a `Gap(block="execution",
code="not_produced", required=True)` accounts for the absence.

### Hash neutrality — why `v1alpha1` stays enough

A blank `runtime_execution_id` never enters the hashed `execution` block:
`ExecutionJoin.to_dict()` emits the key only when non-blank, and `seal()`
additionally prunes it from the redacted payload before hashing (covering
the one path where a blank can reappear post-redaction — `_safe()` rewrites
a dropped leaf to `""` rather than omitting it). Together these make the
hashed `execution` block a pure function of the two identity fields in every
path, so every `we_`-only envelope sealed before this amendment — including
`investigator-evidence.json` — keeps its exact `content_hash` and
`evidence_id`. Because nothing is persisted yet (Tier B is unbuilt) and the
change is provably hash-neutral, `api_version` stays
`evidence.mctl.ai/v1alpha1` and `SUPPORTED_API_VERSIONS` gains no second
entry — bumping would force Tier B to support two document shapes and two
conformance suites on day one for a contract with no data to migrate.

### Golden vectors

Three committed fixtures under `tests/fixtures/evidence/`, each asserted for
literal `content_hash`, literal `evidence_id`, `to_dict()` round-trip and
`recompute_content_hash()` agreement:

| Fixture | Join | `primary_execution_ref` |
| --- | --- | --- |
| `investigator-evidence.json` (unchanged) | `we_` only | `("work", "we_...")` |
| `implementer-evidence.json` (new) | `ex-` only, with `policy_decisions` and an `approvals` entry bound to it | `("runtime", "ex-...")` |
| `shepherd-evidence.json` (new) | both | `("work", "we_...")` |

### Named follow-ups (not built here)

1. The #199 evidence producer: seal an envelope at the end of a governed
   implementer/shepherd workflow, populating `runtime_execution_id` from
   `execution_identity.load_from_environment()` and `execution_id` from
   `work_context.executions.resolve_identity()` when one exists.
2. De-overload `usage_ledger.execution_id` (`:59-66`, `:176`) and mctl-api's
   `model_usage_records.execution_id` into the same two typed fields, so the
   ledger stops being the counter-example this amendment cites.
3. Optionally attach a `we_` to implementer and shepherd runs (model (A) as
   a purely additive producer-side enhancement; no contract change
   required — `shepherd-evidence.json` already pins the resulting shape).
4. Tighten `ExecutionJoin.trace_id` to ADR 011's 32-lowercase-hex shape. Held
   back because the existing golden fixture uses a Temporal workflow id
   there, and tightening it now would move a hash this amendment promises
   not to move.

This amendment reopens nothing else sec. 1–5 fixed: the hash rule, the
redaction-before-hash rule, the derived-`completeness` rule's mechanism, and
every other boundary row are unchanged.
