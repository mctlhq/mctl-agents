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
| `execution` | `ExecutionJoin` | caller | the `we_` execution this evidence is for — required |
| `outcome` | `Outcome` | caller | the final outcome — required, exactly one |
| `policy_decisions` | `PolicyDecisionRef[]` | caller | every policy decision this execution's actions produced — required by default |
| `snapshot_refs` | `SnapshotRef[]` (optional) | caller | `ContextSnapshot`s this execution sealed or consulted; absent when empty |
| `execution_request` | `ExecutionRequestRef \| null` (optional) | caller | the `xr_` request that dispatched this execution, if any |
| `usage` | `UsageRef \| null` (optional) | caller | join keys into the usage ledger, if any |
| `approvals` | `ApprovalRef[]` (optional) | caller | `aar_` approvals this execution's actions relied on |
| `artifacts` | `ArtifactRef[]` (optional) | caller | generated artifacts this execution produced |
| `gaps` | `Gap[]` | `_safe()` + caller | every block known to be missing, and why |

**`ExecutionJoin`** — `execution_id` (`we_`-validated), `work_item_id`,
`trace_id`.

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
| Execution identity (#196) | `orchestrator/work_context/`, ADR 011 | `execution_id`, `work_item_id`, `trace_id` (`ExecutionJoin`) — a reference only | Copy any `ExecutionContext` field beyond the join keys |
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
