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
| `versions` | `VersionPins` (optional, Amendment 2; the key is omitted from `to_dict()` when absent, unlike `usage`'s `null`) | caller | the resolved ADR 007 release pins this execution ran under |
| `subject` | `SubjectRef` (optional, Amendment 2; omitted when absent) | caller | what the evidence is about, bound to the exact revision observed |
| `tool_calls` | `ToolCallRef[]` (optional, Amendment 2; omitted when empty) | caller | consequential tool calls: kind, name, action digest, status |
| `provenance` | `Provenance` (optional, Amendment 2; omitted when absent; required with `subject`) | caller | `authority`, `observed_at`, `supersedes` |

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

**Amendment 2** (mctlhq/mctl-agents#199) additionally changed only:

```
docs/adr/018-execution-evidence-envelope-contract.md    # this amendment; sec. 1 table rows added
orchestrator/execution_evidence.py                      # four optional blocks, observation_failed, resolve_current
orchestrator/policy_checkpoint.py                       # ACTION_KINDS: the governed action kinds as one frozenset
tests/test_execution_evidence.py                        # T16 + extensions to T3, T5, T10
tests/fixtures/evidence/shepherd-pr-evidence.json              # new fixture (all four blocks)
tests/fixtures/evidence/shepherd-pr-superseding-evidence.json  # new fixture (supersedes the above)
```

All three pre-amendment fixtures are unchanged, byte-for-byte, and keep
their literal `content_hash`/`evidence_id`.

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

## Amendment 2 — versions, subject binding, tool calls, authority and supersession (mctlhq/mctl-agents#199)

> **Status:** accepted
> **Date:** 2026-10-04
> **Owner decision:** 2026-10-04 (the evidence #199 must carry, below)

### Why

The 2026-10-04 audit of #199 found the envelope could say *which execution*
produced evidence but not *what the evidence was about*, *under which
release*, *which consequential calls it made*, *how much to trust it* or
*whether it is still current*. Concretely:

- nothing pinned the `AgentDefinition` / `ExecutionProfile` / release
  binding the run executed under, although ADR 007 sec. 5 already
  materializes exactly those pins in `ExecutionPlan`;
- consequential tool calls were visible only indirectly, as a
  `PolicyDecisionRef.action_digest` — a call no checkpoint governed, or one
  whose result was never observed, left no trace at all;
- evidence about PR #524 at SHA1 and PR #524 at SHA2 were indistinguishable
  — nothing in the hashed payload named the revision;
- a model's own claim and an observation read from GitHub had the same
  standing;
- there was no way to say "this envelope replaces that one", and no rule
  for which of several envelopes about the same thing is current;
- "could not observe" was expressible only through gap codes whose
  `required` flag the caller chose freely.

The owner's rules for the fix: reuse Tier A/Tier B, never a second
telemetry system; tracing (#195) is optional enrichment, not a dependency.

### The four new optional blocks

Each is optional at the envelope level, **enters the hashed payload only
when present** (`None` / empty list = absent, the sec. 2 rule) and is
**omitted from `to_dict()` when absent**, so a pre-amendment envelope both
hashes and serializes byte-identically. Inside a present block every key is
always emitted (blank as `""`, `release_revision` as `null`) — the rule
every pre-amendment block already follows, which keeps redaction
(`_REDACTED_LEAF`) reproducible.

**`versions: VersionPins`** — the resolved release, field names copied from
`context_snapshot.ExecutionCorrelation` / `resolver.ExecutionPlan`:

| Field | Validation | Meaning |
| --- | --- | --- |
| `agent` | required; slug `[a-z0-9][a-z0-9._-]*`, ≤128 | the agent name |
| `environment` | optional slug | the binding's environment |
| `definition_version` | optional version token `[A-Za-z0-9][A-Za-z0-9._:+-]*`, ≤128 | registry version — names no bytes (ADR 007 sec. 4) |
| `definition_content_hash` | **required**; `sha256:` + 64 hex | the pin that actually names the definition bytes (`spec.sourceManifest.contentHash`) |
| `profile_name` | optional slug | the `ExecutionProfile` name |
| `profile_version` | optional version token | the profile registry version |
| `profile_content_hash` | optional `sha256:` + 64 hex | the profile bytes read |
| `release_revision` | optional int, `0 ≤ n ≤ 2^63-1` (Tier B stores a signed 64-bit integer) | the `ReleaseBinding` revision |

**`subject: SubjectRef`** — what the evidence is about, bound to a version:

| Field | Validation | Meaning |
| --- | --- | --- |
| `kind` | required; closed `SUBJECT_KINDS` = `pull_request`, `issue`, `branch`, `release`, `work_item` | subject class |
| `repository` | `owner/name`; required for `pull_request`, `issue`, `branch`, `release`; the name half may not be all dots or contain `..` (`.github` is fine) | the GitHub repository |
| `ref` | required; the number for `pull_request`/`issue` (`[1-9][0-9]{0,9}`), otherwise `[A-Za-z0-9][A-Za-z0-9._/+-]*` ≤256 with no `..`, `//` or trailing `/` | PR/issue number, branch, tag or work item id |
| `revision` | **required full lowercase git SHA (40 or 64 hex) for `SHA_BOUND_SUBJECT_KINDS` = `pull_request`, `branch`, `release`**; optional version token otherwise | the exact version observed |

`revision` is in the hashed payload, so **PR@SHA1 and PR@SHA2 can never
share a `content_hash` or `evidence_id`**. A moving pointer without its
revision is rejected, not sealed: evidence about "PR #524" with no SHA is
bound to nothing. If the producer could not read the SHA, it omits
`subject` and records `Gap(block="subject", code="observation_failed",
required=True)`. `SubjectRef.key` = `(kind, repository, ref)` is a derived
property, never stored or hashed.

**`tool_calls: ToolCallRef[]`** — consequential calls, in call order, at
most `MAX_TOOL_CALLS` = 256:

| Field | Validation | Meaning |
| --- | --- | --- |
| `kind` | required; closed `TOOL_CALL_KINDS` = `policy_checkpoint.ACTION_KINDS` itself (one frozenset, reused the way `VERDICTS`/`UNDECIDED_CODES` are) | the action class |
| `name` | optional `[A-Za-z0-9][A-Za-z0-9_.:-]*` ≤128 | the tool / operation name (`ActionRequest.operation`) |
| `action_digest` | required; `sha256:` + 64 hex | `ActionRequest.action_digest()` — the same value `PolicyDecisionRef.action_digest` and an `aar_` `intent_hash` bind, so the three join without carrying arguments |
| `status` | required; closed `succeeded`, `failed`, `refused`, `unknown` | the observed result; `unknown` = the result could not be observed, never folded into `failed` |

A tool call whose digest matches no `PolicyDecisionRef` is legal and is
exactly what the evidence must reveal (an ungoverned call), so the
contract does not cross-require the two.

**`provenance: Provenance`** — required whenever `subject` is present:

| Field | Validation | Meaning |
| --- | --- | --- |
| `authority` | required; closed `AUTHORITIES` = `observed` > `derived` > `asserted` | `observed`: read from the system of record by the producer itself; `derived`: computed deterministically from other records; `asserted`: a model's/agent's own claim |
| `observed_at` | required; same shape as `created_at` | when the recorded state was observed. **Hashed**, unlike `created_at`: it is a fact about the evidence, not about sealing |
| `supersedes` | optional; must fullmatch `ev-[0-9a-f]{16}` and must not be the envelope's own id | the earlier envelope this one explicitly replaces |

The field name `authority` passes the sec. 5 no-authorization test (it
contains no `allow`/`deny`/`permit`/`grant`/`authorized` token) and grants
nothing: it ranks evidence, it never authorizes an action.

### Unknown is not absence

`GAP_CODES` gains `observation_failed`: the producer attempted to observe
the referent and the read failed, was partial or was malformed. It is the
only code in `UNKNOWN_GAP_CODES`, and **`validate()` rejects it unless
`required=True`**, so an unknown can never leave an envelope `COMPLETE`.
The legacy codes keep their caller-chosen flag — tightening
`store_unavailable`/`undecided` retroactively could invalidate an envelope
some caller already sealed. Vocabulary:

| Situation | Express it as |
| --- | --- |
| Observed, and there is nothing (no tool calls, no approval) | leave the optional block absent; optionally `Gap(code="not_applicable", required=False)` |
| Could not observe (API error, partial page, malformed body) | `Gap(code="observation_failed", required=True)` |
| The execution did not produce it | `Gap(code="not_produced", ...)` (unchanged) |
| A tool call ran but its result is unknown | `ToolCallRef(status="unknown")` |

**Redaction inside a new block.** `_safe()` runs over the new blocks like
every other one. A dropped leaf becomes `""` plus a `redacted_out` gap,
and **for the four Amendment 2 blocks that gap is always `required=True`**
(`AMENDMENT_2_BLOCKS` in `_is_block_required`): each block binds
identity or trust, so a partly dropped one must leave the envelope
`INCOMPLETE`. `validate()` then tolerates a blank *required* leaf (for
example `subject.ref` on a branch whose name trips the credential screen)
only when such a gap names that block, so `seal()` degrades to an
explicit gap and never loses the whole envelope. A blank required leaf
with no redaction gap was never supplied and is still rejected, and so is
a non-required `redacted_out` gap on a new block (seal never writes one).

`Requirements` gains `versions`, `subject`, `tool_calls` and `provenance`
flags, all `False` by default, so `DEFAULT_REQUIREMENTS` — and every
envelope sealed under it — is unchanged.

### Current vs historical — `resolve_current`

One rule, implemented as the pure function
`resolve_current(candidates, *, kind, repository, ref, revision)` in Tier A,
which Tier B must reproduce and test against:

0. Every argument is validated with the `subject` rules (a blank
   `revision` excepted). A malformed one, such as an abbreviated or uppercase
   SHA or a `kind` outside `SUBJECT_KINDS`, raises
   `ExecutionEvidenceError`. It is a caller bug, never `no_evidence`.
1. `revision` (the subject's live revision, which the *reader* has just
   observed) blank → `unknown_revision`. Never `no_evidence`.
2. Pool = candidates with exactly this `subject.key` **and** this
   `revision`. Everything at another revision is historical by definition.
3. A pool member named by another pool member's `provenance.supersedes` is
   dropped — but only when the superseding envelope's authority is equal
   or stronger. A link to an envelope outside the pool removes nothing.
4. Empty → `no_evidence`. Otherwise the highest `(authority rank,
   observed_at)` wins: a newer assertion never displaces an older
   observation; among equal authority the later observation wins
   (`observed_at` compared with the fraction normalized, not as raw text).
5. More than one distinct envelope at the top → `ambiguous`, never an
   arbitrary pick. Exactly one → `current`.

`candidates` must be the complete set for the subject key: a reader whose
listing failed or was truncated has an unknown and must not call the rule
(a short list would turn it into `no_evidence`).

### Hash neutrality and version policy

Nothing here changes `canonical_json`, `hash_bytes`, the redaction walk or
the `evidence_id` derivation. Absent blocks never enter the payload, so the
three pre-amendment fixtures keep their literal identities — proven by T3
(literal `content_hash`/`evidence_id` per fixture, files untouched) and by
T16's re-seal test (each legacy fixture re-sealed through the amended
`seal()` reproduces its literal). As with Amendment 1, `api_version` stays
`evidence.mctl.ai/v1alpha1`: the change is additive and hash-neutral, and
no stored envelope can carry the new keys yet — Tier B's strict key check
rejects them today (`evidenceEnvelopeKeys`), so there is nothing to
migrate and no second conformance suite to keep.

### Golden vectors

| Fixture | Shape | `content_hash` |
| --- | --- | --- |
| `shepherd-pr-evidence.json` (new) | both identities; `subject` PR #524 @ a 40-hex SHA; `versions`; one `github.pull_request.merge` tool call; `provenance` `observed`; `COMPLETE` | `sha256:df6d015f8ddf64ac6f1955649883708402c7185618a09b1c07b7276fa32c7cc8` |
| `shepherd-pr-superseding-evidence.json` (new) | same subject; `supersedes` the above; tool call `status: unknown`; `Gap(versions, observation_failed, required)`; `INCOMPLETE` | `sha256:5a7500c45dd74b8b0d46388115813a3b9576237c5523679d0f254e0994c4b282` |

`resolve_current` over the pair at the fixture's SHA returns the
superseding envelope (T16).

### Acceptance criterion 1 of #199

Reworded by owner decision 2026-10-04: evidence for an execution is
reconstructed **from its canonical execution records** (the `we_`/`ex-`
join plus the referenced `cs_`, `xr_`, `aar_`, policy-decision and usage
records). Tracing (#195) is optional enrichment: `ExecutionJoin.trace_id`
stays an optional join key, and no rule here depends on a trace existing.

### Producer call points (mctlhq/mctl-agents#544 — not built here)

The producer seals one envelope per governed run and posts it; it is never
fatal to the run:

- **Where:** at end-of-run of `run_issue_investigator.py`,
  `run_implementer.py` and `run_shepherd.py`, in a `finally` path that runs
  for success, failure and refusal alike (the `outcome` block carries
  which).
- **What:** `seal()` with `runtime_execution_id` from
  `execution_identity.load_from_environment()`, `execution_id` from
  `work_context.executions.resolve_identity()` when one exists, `versions`
  from the run's `ExecutionPlan`, `subject` from the issue/PR the run acted
  on with the revision the producer itself read (else an
  `observation_failed` gap), one `ToolCallRef` per checkpointed action
  (`policy_checkpoint` already computes the digest), and `provenance`
  `observed` for facts the producer read itself, `asserted` for anything
  that is only the model's claim. A re-run that corrects an earlier
  envelope sets `supersedes`.
- **How:** `POST /api/v1/evidence/records` with body
  `{"envelope_b64": base64(<to_dict() JSON>)}` and
  `Authorization: Bearer $MCTL_EVIDENCE_WRITER_TOKEN`. Any exception,
  timeout, non-2xx or unset token is logged and counted, never raised:
  evidence loss is reported, the run's result is not changed by it.
- **Ordering:** the producer must not emit any Amendment 2 block until the
  Tier B follow-up below is deployed — mctl-api answers `400
  evidence_invalid` for unknown keys today. Until then it seals without
  them (the pre-amendment shape, which stays valid).

### Tier B follow-up (mctl-api) — checklist

A follow-up mctl-api PR against `internal/evidence` must, before any
producer emits the new blocks:

1. **Accept the keys.** Add `versions`, `subject`, `tool_calls`,
   `provenance` to `evidenceEnvelopeKeys` and to `optionalBlockKeys`
   (absent when missing, `null` or `[]`; an object block that is present
   always enters the payload — the existing `isEmptyBlock` rule). Reject
   unknown keys inside each block, exactly as Tier A's `from_dict` does.
   Canonicalization is otherwise unchanged.
2. **Re-validate, never trust.** Mirror `_check_versions`,
   `_check_subject` (SHA-bound kinds need a 40/64-hex `revision`),
   `_check_tool_call`, `_check_provenance` (closed `authority`;
   `observed_at` shape; `supersedes` = `^ev-[0-9a-f]{16}$` and ≠ own id),
   "`subject` requires `provenance`", `MAX_TOOL_CALLS`, and
   "`observation_failed` gaps must be `required: true`", "a `redacted_out`
   gap on an Amendment 2 block must be `required: true`, and only such a
   gap excuses a blank required leaf in that block", the `subject.repository`
   path-fragment rule, and `release_revision` within signed 64-bit range.
   Violations answer `400 evidence_invalid`.
3. **Conformance.** Copy `shepherd-pr-evidence.json` and
   `shepherd-pr-superseding-evidence.json` into
   `internal/evidence/testdata/` and assert their literal hashes; the
   existing three vectors keep theirs.
4. **Store (immutable columns, written once at ingest).** `ALTER TABLE
   execution_evidence ADD COLUMN IF NOT EXISTS` (DDL, not an `UPDATE`, so
   the immutability trigger is untouched; existing rows need no backfill —
   none can carry the blocks):
   `subject_kind`, `subject_repository`, `subject_ref`, `subject_revision`,
   `authority`, `supersedes` (`TEXT NOT NULL DEFAULT ''`), `observed_at`
   (`TIMESTAMPTZ NULL`). CHECK constraints: `authority IN ('', 'observed',
   'derived', 'asserted')`; `supersedes = '' OR supersedes ~
   '^ev-[0-9a-f]{16}$'`; `supersedes <> id`; `subject_kind = '' OR
   (authority <> '' AND observed_at IS NOT NULL)`; `subject_kind NOT IN
   ('pull_request','branch','release') OR subject_revision ~
   '^[0-9a-f]{40}([0-9a-f]{24})?$'` (or `''` when a `redacted_out` gap
   excused it, which leaves the row out of every current pool). `versions`
   and `tool_calls` stay in the
   verbatim envelope only — no columns, no second copy.
5. **Index.** `(subject_kind, subject_repository, subject_ref,
   subject_revision, observed_at DESC) WHERE subject_kind <> ''` and
   `(supersedes) WHERE supersedes <> ''`.
6. **Supersession at ingest.** When the named envelope exists, it must have
   the same `(subject_kind, subject_repository, subject_ref,
   subject_revision)` and an authority rank ≤ the new one, else `422
   evidence_supersedes_invalid`. When it does not exist yet, accept (the
   producer never blocks on ordering): the read rule only honours links
   inside the pool, so a dangling link retires nothing.
7. **Expose.** Serve `subject`, `authority`, `observed_at`, `supersedes`
   and a read-time-derived `superseded_by` (valid links only) on every
   evidence record; add `subject_kind`/`subject_repository`/`subject_ref`/
   `subject_revision` filters to `GET /api/v1/evidence` and
   `GET /api/v1/work-items/{id}/evidence`.
8. **Current read.** `GET /api/v1/evidence/current?subject_kind=&repository=&ref=&revision=`
   implementing `resolve_current` exactly, including the argument
   validation (a malformed parameter answers `400`, never `no_evidence`),
   and answering `{state, evidence}`
   with `state` ∈ `current`, `no_evidence`, `unknown_revision`
   (missing `revision`), `ambiguous`. It loads the complete pool; a pool
   larger than the server cap, or any read error, is a `5xx`/typed error —
   never a truncated `no_evidence`. Same authorization as `GET
   /api/v1/evidence` (admin), plus the work-item-scoped variant through
   `visibleWorkItem`.
9. **Test both ways.** Each rule above green on a valid envelope and red on
   a deliberate mutation (reverting it), per the workspace detector rule.

### What this amendment does not change

`to_log_dict()` gains `tool_call_count`, `subject_kind` and `authority`:
a count and two closed-vocabulary codes, never ids.

The hash rule, the redaction-before-hash rule, the derived `completeness`
mechanism, the boundary table (evidence still grants nothing; persistence
is still Tier B), Amendment 1's two typed identities, and every
pre-amendment block's shape and validation.
