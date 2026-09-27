# ADR 015 — `ExecutionEvidence` contract for auditable governed runs

> **Status:** proposed
> **Date:** 2026-09-23
> **Issue:** mctlhq/mctl-agents#199 (related: #195 traces, #196 execution
> identity, #197 policy checkpoint, #198 approvals, #60; mctl-api#366)

## Context

mctl-agents now has four of the five inputs an after-the-fact audit needs,
and none of them is joined to the others.

- **Traces (#195)** exist in code (`orchestrator/tracing.py`,
  `orchestrator/tracing_sdk.py`) but nothing is exported in production: the
  platform Collector has no trace backend (mctl-gitops#1280, open), the
  2048-span queue drops spans under load by design, and the export guard
  (`tracing_sdk.GuardedExporter`) deliberately strips the target and both
  digests of a policy decision before it could leave the process
  (`tracing.py`'s `record_policy_decision`: "never the target").
- **Execution identity (#196, ADR 011)** is sealed and content-addressed
  (`orchestrator/execution_identity.py`): `api_version:
  identity.mctl.ai/v1alpha1`, `context_id = "ex-" + content_hash[7:23]`.
  Answers *who is executing*; never *what they may do*.
- **Policy decisions (#197, ADR 014)** are evaluated fail-closed and printed
  as one `POLICY_DECISION <json>` line per decision
  (`orchestrator/policy_checkpoint.py`), carrying exactly the fields an audit
  record needs — including the target and both digests the trace event
  drops — but the line lives only in pod logs, which rotate.
- **Durable approvals (#198, mctl-api#366)** are redeemed atomically at
  decision time (`orchestrator/action_approvals.py`); the receipt id is the
  only trace of who approved what, and it lives in mctl-api, not here.
- **Model usage/cost (ADR 012)** is not implemented. No module reads
  `total_cost_usd` or `model_usage`; the only model data that exists is the
  token counters `tracing.AgentRunObserver` already reads off SDK messages.

ADR 009 already reserved the seam this ADR fills:
`ContextSnapshot.evidence_refs: tuple[EvidenceRef, ...]` where
`EvidenceRef{evidence_id, kind}` and nothing else
(`orchestrator/context_snapshot.py:710`), with the boundary row "Evidence
(#199) | Evidence store | `evidence_refs: {evidence_id, kind}` only | Inline
a payload; evidence has exactly one owner" (ADR 009 sec. 5). ADR 011 sec. 6
has the mirror row: `ExecutionContext` records nothing about evidence. This
ADR is what fills that reserved slot: one immutable, versioned,
content-addressed document per governed execution, sealed the same way
`execution_identity.py` and `context_snapshot.py` already seal theirs.

## Decision

### 1. Canonical shape — `evidence.mctl.ai/v1alpha1`, `kind: ExecutionEvidence`

Implemented in `orchestrator/execution_evidence.py`, a stdlib-only module
mirroring `orchestrator/context_snapshot.py` and
`orchestrator/execution_identity.py` line for line in style: module
constants `API_VERSION`/`KIND`/`SUPPORTED_API_VERSIONS`, closed
`frozenset` vocabularies, frozen dataclasses each with `to_dict`/
`from_dict`, and `from_dict` rejecting any key the contract does not
declare and any unsupported `api_version` — the same
`_reject_unknown_keys`/`SUPPORTED_API_VERSIONS.get(...)` pattern
`execution_identity.ExecutionContext.from_dict` uses.

The document carries `api_version`, `kind`, `evidence_id`, `content_hash`,
`created_at`, and nine sections:

| Section | Shape | Source |
|---|---|---|
| `execution` | `trace_id`, `context_id`, `parent_context_id`, `workflow_type`, `step_sequence`, `temporal_workflow_id`, `temporal_run_id`, `argo_workflow_name`, `attempt`, `work_item_id`, `store_execution_id`, `started_at`, `completed_at`, `duration_ms` | `ExecutionContext` + `Correlation`, `work_context.executions` |
| `identity` | `actor{type,id,verification}`, `executor{type,id,agent,version,image_ref,binding}`, `environment`, `tenant`, `repository`, `target_repository_sha`, `context_trust` | `Actor`/`Executor`/`Scope`; `context_trust` from whether a control-plane context loaded |
| `models[]` | `provider`, `model`, `turns`, `input_tokens`, `output_tokens`, `usage_record_ref` | `tracing.AgentRunObserver`'s fixed vocabulary; `usage_record_ref` empty until ADR 012 lands |
| `actions[]` | `sequence`, `action_kind`, `operation`, `target_ref`, `target_digest`, `args_digest`, `action_digest`, `policy_version`, `rule_id`, `decision`, `code`, `permitted`, `undecided`, `mutation`, `approval_ref`, `at` | one per `policy_checkpoint.Decision` |
| `approvals[]` | `receipt_id`, `intent_hash`, `state`, `approver`, `requested_at`, `decided_at`, `consumed_at` | `action_approvals.ApprovalRecord` / `GET /action-approvals/{id}` |
| `artifacts[]` | `name`, `kind`, `content_hash`, `byte_count`, `locator`, `immutable_ref` | proposal files, branch, PR, merge commit |
| `evaluations[]` | `name`, `result` (`PASS`/`FAIL`/`SKIP`), `code` | built-in `policy_compliance`, `evidence_completeness`; open for #60 |
| `completeness` | `status` (`COMPLETE`/`INCOMPLETE`), `gaps[]{code, action_kind, operation, detail_code}` | `check_completeness()` |
| `outcome` | `status` (`SUCCESS`/`FAILURE`/`REFUSED`/`UNDECIDED`/`SKIPPED`), `code` | the run's own result |
| `retention` | `class` (`telemetry`/`execution-record`/`gitops`, ADR 009's vocabulary reused verbatim), `expires_after_days` | this ADR's default: `gitops`, 3650 days |

No field is a free-text message, a prompt, a completion, a tool argument or
result, an issue/comment body, an argv, a commit message, file contents, or
stdout/stderr. `ActionRecord` has no `reason` field — `policy_checkpoint`'s
free-text `Decision.reason` is deliberately not carried, because it can
quote an exception's message.

### 2. Identity and immutability

`content_hash = "sha256:" + sha256(canonical JSON of every field except
content_hash, evidence_id, created_at)`, using
`context_snapshot.canonical_json` / `context_snapshot.hash_bytes` — the same
convention ADR 009 and ADR 011 already fix, reused rather than
reimplemented. `evidence_id = "ev-" + content_hash[7:23]` — derived from
content, never random. `seal()` is the only constructor that fills
`content_hash` and `evidence_id`; there is no mutator. `created_at` is
excluded from the hash, so sealing the same inputs twice at different clock
times yields the same identity (`tests/test_execution_evidence.py`'s
`test_seal_is_deterministic`). A correction is a new document with a new
`evidence_id`, never an edit.

`recompute_content_hash(record)` reproduces `content_hash` from a loaded
document's own fields; `is_trustworthy(record)` also rechecks the
`evidence_id` binding, the same double check
`execution_identity.load_from_environment` makes for `context_id`. **A
document whose declared `content_hash` disagrees with a fresh recompute is
untrusted**, and a reader must report it as such rather than returning it as
evidence — the same fail-closed rule ADR 011's tamper-evidence check
enforces on `MCTL_EXECUTION_CONTEXT_FILE`.

### 3. Redaction — `_safe()`, mirroring the export guard

`orchestrator/tracing_sdk.py`'s `GuardedExporter` already states the rule
this ADR needs: drop (never mask, never truncate) a string over 256
characters, or matching a credential shape (`ghp_`/`ghs_`/`github_pat_`,
`sk-`, a Vault token `hv[sbr].`, a JWT, a PEM private-key block, a `Bearer `
header, or a `user:pass@` URL credential). Evidence does not go through
`GuardedExporter` — it never reaches the trace pipeline at all — so
`execution_evidence._safe()` carries its own literal copy of the same regex
and length bound rather than importing `tracing_sdk` (which pulls in the
OTel SDK at module scope, exactly what a stdlib-only contract module must
not do). A string that fails the check is dropped from the field entirely,
never truncated (a truncated payload is still a payload) and never masked (a
masked value reads like data that was captured, the Collector's `****`
problem `tracing_sdk.py` already names).

**`target_ref` / `target_digest` split.** A `target` is admitted as
`target_ref` only when it matches a closed, already-public allowlist: a
`github.com` issue/PR URL, `owner/repo`, `owner/repo:<ref>`, an
`aar_`/`we_`/`ex-`/`cs-`-prefixed id, or an mctl-api operation-id shape
(`<verb>:<slug>`, e.g. `policy_checkpoint.BUILTIN_POLICY`'s own
`execute:mctl-agents-investigate`). Anything else is reduced to
`target_digest` — `sha256:` + hex over the raw string via
`context_snapshot.hash_bytes` — and `target_ref` is dropped. The two fields
are mutually exclusive; `ExecutionEvidence.validate()` rejects a document
that sets both.

### 4. `EvidenceRecorder` — never-raises, opt-in collector

`EvidenceRecorder` collects `offer_decision`/`offer_artifact`/
`offer_model_turn`/`offer_approval` calls from the code that already
performs the corresponding action — a governed action is recorded once, by
the site that performs it, never by a second instrumentation layer — and
`finish()`s them into one sealed document. Every method is total: it never
raises, regardless of what it is given, because "evidence never fails a
run" (sec. 7 below) has to hold even when the recorder itself is fed
garbage. The active recorder is an optional module-level sink
(`set_recorder`/`get_recorder`), unset by default, so an unwired call site's
offer costs exactly one attribute read and changes nothing about today's
behaviour. `policy_checkpoint._emit`, `tracing.record_artifact`'s call
sites, `AgentRunObserver` and the approval lookup are the intended future
callers (tasks 7-10 of this proposal's tasks.md); none of them is wired in
this slice — see "Follow-ups" below.

### 5. Completeness — `check_completeness()`

A recorded mutation is either an `ActionRecord` with `mutation=True` (a
GitHub-mutating or `mctl.work_item.write` action kind, the same
classification `tracing.classify_command`'s `_GH_MUTATING` set encodes for
`gh ... comment`/`create`/`merge`/etc.) or an `ArtifactRecord` of kind
`branch`, `pull_request` or `merge_commit`. Gap codes:

| Code | Raised when |
|---|---|
| `mutation_without_decision` | a mutating-kind artifact exists but no recorded action is both `mutation=True` and `permitted` |
| `approval_unresolved` | an action carries `code: approved` and an `approval_ref`, but no `ApprovalRecordRef` with that receipt id was recorded |
| `decision_without_outcome` | an approved action's receipt WAS resolved, but its `consumed_at` is empty — approved, but not confirmed spent |
| `identity_unavailable` | `identity.context_trust != "control-plane"` (no control-plane context was loadable) |
| `ungoverned_transport` | a **standing** gap, supplied by the caller (not detected from the record) for any run whose builder grants `Bash` (ADR 014 open decision 4) |

`completeness.status` is `COMPLETE` **iff** `gaps` is empty —
`ExecutionEvidence.validate()` enforces the iff, not just the "gaps present
implies INCOMPLETE" direction. `ungoverned_transport` is deliberately a
caller-supplied standing flag rather than something the checker infers: the
checkpoint governs the MCP transport and the orchestrator's own GitHub
mutations, never the agent's own shell (ADR 014 sec. "Open decisions" item
4), so a builder that grants `Bash` can never honestly claim full coverage,
and this ADR does not pretend otherwise.

**Built-in evaluations.** `policy_compliance` is `PASS` only if every
`mutation=True` action is `permitted` **and** `completeness.status ==
COMPLETE`; `evidence_completeness` is `PASS` iff `completeness.status ==
COMPLETE`. Both are `Evaluation{name, result, code}` entries in
`evaluations[]`; #60 (an external compliance-report proposal) may add more
entries there without a schema change.

**Undecided is not refused.** An action whose `code` is one of
`policy_checkpoint`'s `UNDECIDED_CODES` (`evaluator_error`,
`identity_unavailable`, `approval_lookup_error` — the checkpoint *could not
answer*, not that it said no) is recorded with `undecided=True`, and no
undecided code by itself sets `outcome.status` to `REFUSED`. Conflating "the
checkpoint could not decide" with "the checkpoint refused" would make a
transient approval-store outage look, in the audit record, exactly like a
human policy refusal — the two demand different responses and must stay
distinguishable in the record that outlives the incident.

### 6. Boundary rules — normative and testable

| Concern | Owner | What `ExecutionEvidence` may record | What it must never do |
|---|---|---|---|
| Authorization (#197) | Policy checkpoints + provider-side enforcement | A **copy** of a decision `policy_checkpoint.decide()` already made — `code`, `decision`, `permitted`, `rule_id`, `policy_version` | Re-decide, re-evaluate, or change any authorization outcome; `check_completeness()` only notices an *absence* of a decision, it never substitutes one |
| Context relevance (ADR 009) | `ContextSnapshot` | `evidence_refs: {evidence_id, kind}` on the snapshot side only | Be inlined into `ContextSnapshot`; no field of `ExecutionEvidence` may be copied into `ContextSnapshot.sources` |
| Identity (ADR 011) | `ExecutionContext` | A projection of `actor`/`executor`/`scope` into `identity` | Be inlined into `ExecutionContext`; `execution_identity.py` names no evidence field (ADR 011 sec. 6) and this ADR does not add one |
| Traces (#195) | Trace/telemetry pipeline | Nothing new — `trace_id` is the join key both directions already share | Depend on a trace existing or being exported; this document is sealed and persisted independently of whether tracing is configured |

Two tests make this executable rather than aspirational
(`tests/test_execution_evidence.py`): a recursive scan of a sealed record
built from inputs carrying a credential-shaped string, a JWT and an
oversized string, asserting none of them appear anywhere in the serialized
document; and the completeness/evaluation tests (T4-T7) that make
`mutation_without_decision`, `approval_unresolved`, the undecided codes and
`ungoverned_transport` executable rules rather than prose.

**Evidence never re-decides anything, and no other contract inlines an
evidence payload** — the same two normative rules ADR 009 sec. 5 and ADR
011 sec. 6 state for their own boundaries, extended to cover this ADR's own
document.

### 7. Storage, retrieval, export, and failure isolation

`orchestrator/evidence_store.py` persists the sealed, canonical JSON
document under
`platform-gitops/agents-state/_evidence/<workflow_type>/<workflow_id>/<attempt>-<evidence_id>.json`,
plus a `by-trace/<trace_id>/<evidence_id>` pointer file (content: the record
path) — the gitops agents-state tree, because it is the only durable store
every agent pod already writes to, and because ADR 009 sec. 6 already names
`gitops` as a retention class. Every path is content-addressed and
per-record, so two concurrent executions never contend on a shared index.
Retrieval by workflow id, trace id or evidence id returns every matching
document, newest attempt first. Export is the canonical JSON of the sealed
document unchanged: an exported document's bytes, re-parsed and rehashed,
reproduce its own `content_hash` — an exported document is self-verifying,
and a caller (the `python -m orchestrator.evidence_store show` CLI) that
cannot verify one reports it as untrusted rather than printing it as
evidence.

**Failure isolation.** Sealing, persisting and emitting evidence are wrapped
so that any failure logs once and lets the run continue with its own result
and exit code unchanged: evidence assembly can never become a new way for a
governed run to fail. `EvidenceRecorder.finish()` returns `None` rather than
raising when sealing cannot complete; a caller must treat `None` exactly
like a failed `seal()`.

## What this ADR does not decide

- **Trace backend choice** (mctl-gitops#1280). Traces stay the "what is
  happening" view; `trace_id` is the join key between the two documents, not
  a dependency of either on the other. This ADR's document is sealed and
  persisted whether or not a trace backend exists.
- **The usage ledger** (ADR 012). `ModelUse.usage_record_ref` is this
  contract's reserved slot for it; nothing here computes a cost.
- **The mctl-api evidence store**, its HTTP surface, or its retention
  enforcement. This ADR pins the schema, the id format and the export bytes
  so that a `POST .../evidence` / `GET /api/v1/evidence/{evidence_id}` store
  — shaped byte-for-byte on `orchestrator/work_context/snapshots.py`'s
  `canonical_b64` + `content_hash` upload pattern — is an additive import,
  not a redesign, when it is built. This proposal's own gitops-backed store
  (sec. 7) is Tier A; that store is Tier B, and is not built here.
- **The #198 Temporal approval wait**, the approval UI, and the mctl-api
  signal. Only the receipt fields this ADR's `ApprovalRecordRef` already
  names are read.
- **Governing the agent's own Bash transport** (ADR 014 open decision 4).
  `GAP_UNGOVERNED_TRANSPORT` makes the blind spot visible in the record; it
  does not close it.
- **A dashboard, UI or compliance report over evidence documents.**

## Follow-ups

- Wire `EvidenceRecorder` into `policy_checkpoint._emit`,
  `tracing.record_artifact`'s call sites, `AgentRunObserver`, and the run
  entrypoints (`run_issue_investigator.py`, `run_implementer.py`,
  `run_shepherd.py`) so a governed run actually seals and persists a
  document — this proposal's tasks 7-10, 13-14, deferred to keep the
  initial change a coherent, independently testable unit (the contract
  module plus its own tests) rather than touching five production call
  sites in one PR.
- `docs/observability/execution-evidence.md`, cross-linked from
  `docs/observability/execution-traces.md` (task 15).
- The mctl-api evidence store described above ("What this ADR does not
  decide").
