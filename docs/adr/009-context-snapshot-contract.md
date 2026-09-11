# ADR 009 — `ContextSnapshot` contract and execution correlation

> **Status:** accepted
> **Date:** 2026-09-11
> **Supersedes:** nothing — this is a design document; the only code that
> ships with it is a new, inert, additive schema module

## Context

ADR 007 (`docs/adr/007-agent-definition-execution-profile-contract.md`) and
`orchestrator/resolver.py` already give a precise answer to "what contract
ran": `execute(agent, task)` materializes one immutable `ExecutionPlan`
(`orchestrator/resolver.py:226`) carrying definition/profile versions,
content hashes, model policy version, prompt/skill hashes, tools,
permissions, budget, timeout, sandbox and `target_repository_sha`. What the
repository has no contract for at all is the other half of reproducibility:
**what information was actually put in front of the model.**

Concretely:

- `orchestrator/run_issue_investigator.py` fetches exactly five issue fields
  with `gh_issue_view` (`run_issue_investigator.py:856`: `number,title,
  body,state,url` — no labels, no comments, no linked issues), clones the
  target repo with `_clone_repo` (`run_issue_investigator.py:888`) using
  `--depth=1` and no ref argument, and pins the SHA post-hoc with
  `_target_repository_sha` (`run_issue_investigator.py:105`) — only in
  `declarative` resolver mode; in the default `legacy` mode the SHA is never
  recorded. It sanitizes untrusted text with `_neutralize_prompt_tags`
  (`run_issue_investigator.py:1086`) and wraps issue title/body in
  `<issue_title>`/`<issue_body>` blocks declared "untrusted DATA"
  (`run_issue_investigator.py:1137`). The agent then free-roams the cloned
  tree with Glob/Grep/Read — nothing records which files it actually read.
- `orchestrator/run_incident_responder.py` instructs the agent to call
  `mctl_get_incident` and `mctl_get_service_logs` for "the last ~50 lines"
  (`run_incident_responder.py:114-116`) — a Loki window whose bounds are
  chosen by the model and recorded nowhere.
- `docs/agent-inventory.yaml` names this gap explicitly in its
  `promptSources` vs `runtimeContextInputs` note (lines 40-66):
  `runtimeContextInputs` "are real prompt surface but resolve per run,
  outside this repo… pinned per-execution instead, by the target repo git
  SHA recorded on the execution record."
- `orchestrator/temporal/activities/state.py:36-41` documents that even that
  one pin is incomplete in production: `ExecutionRecord.target_repo` records
  which repo, but "the exact SHA isn't captured here yet: no CWFT exposes it
  as a workflow output today."

So today the answer to "what did the agent see" is one git SHA (sometimes),
plus whatever the model chose to read, chosen without any recorded
vocabulary for source, freshness, trust, budget or selection, and with no
identifier to correlate a snapshot with the execution that produced it —
because there is no snapshot.

This issue (mctlhq/mctl-agents#264) is architecture-first: define the
canonical, versioned `ContextSnapshot` contract, its identity/hash
semantics, its provenance and budget/freshness/trust vocabulary, and its
boundary with `ExecutionProfile` (capability eligibility, mctlhq/mctl-agents
#242), policy (#197), evidence (#199) and traces (#195) — **before** any
retrieval or ranking logic exists, exactly as ADR 007 defined the
definition/profile/plan seam before #227's resolver was built against it.
The hard invariant this proposal must make testable: **context relevance is
never an authorization mechanism.**

The deliverable is this ADR plus `orchestrator/context_snapshot.py`, a
typed, stdlib-only, frozen-dataclass schema and serialization module with
fixtures and tests. It changes no agent's behaviour: no agent produces a
snapshot as part of this proposal, and `orchestrator/resolver.py` is
untouched.

## Decision

### 1. Canonical shape — `context.mctl.ai/v1alpha1`, `kind: ContextSnapshot`

A `ContextSnapshot` is a frozen, JSON-primitives-only document. Every field
below is owned by exactly one component and every hash carries the
`sha256:` prefix (see sec. 3).

**`ContextSnapshot`**

| Field | Type | Owner | Meaning |
|---|---|---|---|
| `api_version` | str | this contract | always `context.mctl.ai/v1alpha1` |
| `kind` | str | this contract | always `ContextSnapshot` |
| `snapshot_id` | str | `seal()` | `"cs-" + content_hash[7:23]`, derived not random |
| `content_hash` | str | `seal()` | `"sha256:..."` over every other field except `created_at` |
| `created_at` | str (ISO-8601) | caller | assembly time; excluded from the hash |
| `execution` | `ExecutionCorrelation` | caller (from `ExecutionPlan` + workflow identity) | joins the snapshot to the run that produced it |
| `step` | `StepRef \| null` | caller | `null` for a per-execution root snapshot; set for a per-step child |
| `strategy` | `ContextStrategy` | assembler | which assembly/ranking logic produced this snapshot |
| `budget` | `ContextBudget` | assembler | the model-independent assembly budget and what was actually used |
| `sources` | `ContextSource[]` | assembler | every source considered, included or not |
| `evidence_refs` | `EvidenceRef[]` | assembler, #199 owns the referent | pointers into the evidence store only |
| `retention` | `RetentionPolicy` | assembler + the store that persists it | which store honours this snapshot and for how long |

**`ExecutionCorrelation`**

| Field | Type | Meaning |
|---|---|---|
| `agent` | str | agent name, e.g. `issue-investigator` |
| `environment` | str | `production` (`dev_loop.py:69`) or `shadow` |
| `temporal_workflow_id` | str | matches `issue_ref.workflow_id_for` — `dev-loop-mctlhq-<repo>-<number>` |
| `temporal_run_id` | str \| null | Temporal run id, when known |
| `argo_workflow_name` | str \| null | matches `ExecutionRecord.argo_workflow_name` |
| `target_repository_sha` | str | the target repo's HEAD SHA, as `ExecutionPlan.target_repository_sha` |
| `definition_version` | str | copied from `ExecutionPlan.definition_version` |
| `definition_content_hash` | str | copied from `ExecutionPlan.definition_content_hash`; `sha256:`-prefixed |
| `profile_version` | str | copied from `ExecutionPlan.profile_version` |
| `profile_content_hash` | str | copied from `ExecutionPlan.profile_content_hash`; `sha256:`-prefixed |
| `release_revision` | int | copied from `ExecutionPlan.release_revision` |

**`StepRef`** — `parent_snapshot_id: str`, `step: str`, `sequence: int`.

**`ContextStrategy`** — `name: str`, `version: str`, `ranker_name: str \|
null`, `ranker_version: str \| null`.

**`ContextBudget`** — `max_sources: int`, `max_bytes: int`,
`max_bytes_per_source: int`, `used_sources: int`, `used_bytes: int`,
`truncated: bool`. No token or context-window field exists or may be added
under this `apiVersion` (sec. 6).

**`ContextSource`** — the provenance descriptor, the heart of the contract:

| Field | Type | Meaning |
|---|---|---|
| `source_id` | str | stable within the snapshot (`"s1"`, `"issue"`, ...) |
| `kind` | str | closed vocabulary — see sec. 6 |
| `locator` | str | non-secret, structured address (bounded length, sec. 7) |
| `selector` | object | what slice was taken (bounded length, sec. 7) |
| `content_hash` | str | `sha256:...` of the bytes actually placed in context |
| `byte_count` | int | size of those bytes |
| `retrieved_at` | str (ISO-8601) | when the source was fetched |
| `freshness` | `Freshness` | `observed_at`, `staleness`, `max_age_seconds \| null` |
| `trust` | `Trust` | `tier`, `rationale_code` |
| `selection` | `Selection` | `rank`, `score \| null`, `strategy_step \| null`, `reason_code`, `included` |
| `redaction` | `Redaction` | `applied`, `rules[]`, `dropped_bytes` |

**`EvidenceRef`** — `evidence_id: str`, `kind: str`. Nothing else; #199 owns
the referent.

**`RetentionPolicy`** — `class: "telemetry" \| "execution-record" \|
"gitops"`, `expires_after_days: int`.

No field in this shape may hold a source payload: there is no such field
declared, and `orchestrator/context_snapshot.py`'s `from_dict` rejects any
key it does not know about — a future caller cannot smuggle one in.

### 2. Identity and immutability

`content_hash = "sha256:" + sha256(canonical JSON of every field except
content_hash, snapshot_id, created_at)`, using `json.dumps(payload,
sort_keys=True, separators=(",", ":"))` — the same `"sha256:"`-prefixed
convention `orchestrator/resolver.py:301`'s `_hash_bytes` already uses for
`ExecutionPlan`. `snapshot_id = "cs-" + content_hash[7:23]` (the 16 hex
characters immediately after the prefix) — derived from content, never
random, so identity is reproducible from the document alone.

`seal()` is the only constructor that fills `content_hash` and
`snapshot_id`; there is no mutator. Two independent implementations reading
only this ADR and hashing the same field values produce the same
`content_hash` — the hash is independent of `created_at`, so re-assembling
identical inputs at a different wall-clock time yields an identical
identity, matching the "created once and never follows later promotions"
rule ADR 007 states for its own runtime snapshot
(`docs/adr/007-agent-definition-execution-profile-contract.md:222`). A
change to any other field produces a new snapshot with a new `content_hash`,
never an in-place edit.

**Every hash in this schema carries the `sha256:` prefix**, with one named,
non-inherited exception: `orchestrator/resolver.py:894`'s `skill_hashes`
emits bare hex without the prefix every other hash in that file carries.
`ContextSnapshot` does not repeat that inconsistency — `content_hash`
(top-level and per-source), `definition_content_hash` and
`profile_content_hash` are always `"sha256:..."`, and
`orchestrator/context_snapshot.py`'s `_require_sha256` helper enforces it at
parse time.

### 3. Lifecycle and granularity

`assembled -> sealed -> referenced -> expired`:

- **assembled** — a caller has fetched and hashed zero or more sources and
  built the not-yet-hashed field values; this state has no representation
  in the schema itself, only in a future producer's working memory.
- **sealed** — `seal()` has computed `content_hash`/`snapshot_id` and
  `validate()` has passed; this is the only state `ContextSnapshot`
  instances represent.
- **referenced** — a sealed snapshot's `snapshot_id`/`content_hash` is
  recorded by a trace (#195), a `StepRef.parent_snapshot_id`, or a durable
  store (sec. 7).
- **expired** — the owning store deletes the snapshot per its
  `retention.expires_after_days`; expiry destroys the snapshot rather than
  truncating it in place (sec. 7).

**Per execution, per step, or both — the answer is both, via parent
reference, and this is unambiguous.** A root snapshot has `step: null`. A
step snapshot carries `StepRef{parent_snapshot_id, step, sequence}` and its
`execution` block MUST equal its parent's byte-for-byte —
`ContextSnapshot.validate(parent=...)` enforces this, and
`validate_step_sequence()` enforces that `sequence` is strictly increasing
among siblings sharing one parent. This maps directly onto DevLoop's
investigate/implement/review-fix phases
(`orchestrator/temporal/workflows/dev_loop.py:534-539` investigate,
`:596-683` implement, `:976-997` the in-loop shepherd tick that drives
review-fix cycles): each phase that assembles its own context gets its own
child snapshot chained to one execution root; a single-step agent emits
exactly one root snapshot and no children.

### 4. Correlation with execution identity

The `execution` block is deliberately a superset of what exists today, so
the join key is "the fields both sides already have," not a new
correlation id: `temporal_workflow_id` matches `workflow_id_for`
(`orchestrator/temporal/issue_ref.py:30`, e.g.
`dev-loop-mctlhq-mctl-agents-264`) and the value `_record` already sends
(`dev_loop.py:327-336`); `argo_workflow_name` matches
`ExecutionRecord.argo_workflow_name`; the four version/hash pins and
`target_repository_sha` are copied straight off `ExecutionPlan`
(`orchestrator/resolver.py:237-256`). Recording `target_repository_sha` on
the snapshot also closes, for context (not for the execution ledger), the
gap `orchestrator/temporal/activities/state.py:36-41` documents: the
investigator already computes the SHA locally
(`run_issue_investigator.py:105`); a future producer can carry it onto the
snapshot even though no CWFT exposes it as a workflow output yet.

### 5. Boundary rules — normative and testable

| Concern | Owner | What `ContextSnapshot` may record | What it must never do |
|---|---|---|---|
| Capability eligibility (mctlhq/mctl-agents#242) | `ExecutionProfile.tools` / `permissions` (`resolver.py:174-181`) | Which capability's output produced a source (`kind`, `locator`) | Widen eligibility; a source recorded here grants no tool access |
| Authorization (#197) | Policy checkpoints + provider-side enforcement | Nothing | No allow/deny/permit/grant/authorized field exists anywhere in the schema; no policy-decision path may import `orchestrator/context_snapshot.py` |
| Evidence (#199) | Evidence store | `evidence_refs: {evidence_id, kind}` only | Inline a payload; evidence has exactly one owner |
| Traces (#195) | Trace/telemetry pipeline | `snapshot_id`, `content_hash`, strategy/ranker name+version, counts, byte totals (`to_log_dict()`) | Emit a `locator`, a `selector`, or any string derived from a retrieved payload |

**Context relevance is never an authorization mechanism.** Echoing ADR
007's own rule that `tools` is not authorization
(`docs/adr/007-agent-definition-execution-profile-contract.md:106-110`): a
ranker surfacing a source, or a caller reading `selection.rank`/`score`,
must never be treated as "the caller may use it." Two tests make this
executable rather than aspirational
(`tests/test_context_snapshot.py`): a recursive field-name assertion that no
`allow`/`deny`/`permit`/`grant`/`authorized` token appears anywhere in the
serialized schema, and a subprocess import-direction assertion (the style of
`tests/test_worker_isolation.py`) proving `orchestrator/context_snapshot.py`
loads stdlib only.

### 6. Vocabularies

**Budget — sources and bytes, never tokens.** `ContextBudget` is
model-independent by construction: `max_sources`, `max_bytes`,
`max_bytes_per_source`, plus observed `used_sources`/`used_bytes` and a
`truncated` flag. This is the assembly budget, **not** the model's maximum
context window. Token-denominated budgets are explicitly deferred: token
counts depend on a tokenizer and model that `config/model-policy.yaml` may
change under an already-sealed snapshot, which would make a historical
budget uninterpretable, and this repository has no tokenizer dependency
today. A future `max_tokens`/`used_tokens` pair can be added as optional
fields without an `apiVersion` bump — the same pattern `DevLoopResult`
already uses for field growth (`dev_loop.py:240,246`) — but no such field
exists yet, and `from_dict`'s unknown-key rejection is what keeps a document
from smuggling one in under this version.

**Freshness — `fresh | aging | stale | unknown`, defaulting to
`unknown`.** A source with no declared `max_age_seconds` never defaults to
`fresh`; the fail-safe default is `unknown`
(`orchestrator/context_snapshot.py`'s `Freshness.from_dict`). Worked
examples from this repo's own agents: a Loki tail
(`run_incident_responder.py:116`) is `fresh` with `max_age_seconds: 3600`; a
repo clone is `fresh` at its pinned SHA; a stale incident record is `aging`
or `stale`.

**Trust — `authoritative | corroborated | reported | untrusted`, origin
only, grants nothing.** `authoritative`: GitOps catalog, registry, or repo
content at a pinned SHA. `corroborated`: platform telemetry such as a Loki
tail or an incident record. `reported`: human-authored platform state (a
maintainer comment). `untrusted`: arbitrary third-party text — a GitHub
issue body is the worked example, consistent with the existing prompt
hardening in `_neutralize_prompt_tags` (`run_issue_investigator.py:1086`)
and the untrusted-DATA wrapper in `_build_prompt`
(`run_issue_investigator.py:1137`): this tier is not new policy, it is that
existing behaviour finally given a name in the data model. Raising a tier
grants nothing — see sec. 5.

**Source kind** — closed set: `github-issue`, `github-issue-comment`,
`github-pr`, `target-repo`, `gitops-file`, `proposal-dir`, `loki-logs`,
`incident`, `inline-template`.

### 7. Sensitive data, telemetry, and retention

**Hash after redaction, never before.** `content_hash` describes the bytes
the model actually saw, so redaction (when it exists — see below) must run
before hashing; the snapshot never needs the raw payload to be verifiable.
`Redaction.rules` records rule ids, never matched text; `dropped_bytes`
records volume only.

**No payload, ever, anywhere.** The schema has no field for one:
`from_dict` rejects unknown keys, and `locator`/`selector` are bounded —
`MAX_LOCATOR_LENGTH` and `MAX_SELECTOR_JSON_LENGTH` (both 2048) in
`orchestrator/context_snapshot.py` — so neither can be abused as an
unbounded free-text carrier.

**Retention** maps onto ADR 007's source-of-truth table
(`docs/adr/007-agent-definition-execution-profile-contract.md:215-224`):
`telemetry` (traces/metrics — shortest-lived, hashes and counts only, #195's
store), `execution-record` (mctl-api, outliving Temporal/Argo retention —
the durable statement, alongside `ExecutionRecord`), `gitops` (only when a
snapshot is committed beside a proposal — longest-lived and therefore most
restricted). Expiry **destroys** the snapshot; it does not truncate it in
place.

**No redaction helper exists in this repository today.** `grep` for a
redact/sanitize/mask/scrub helper returns no production hit;
`_neutralize_prompt_tags` strips delimiter tags only, and
`orchestrator/options.py`'s `_audit_pre_tool_use` prints whole Bash commands
verbatim. The `redaction` block is a contract for a redactor that does not
exist yet — this ADR states that plainly rather than implying a capability
this codebase does not have (see sec. 8's follow-up (c)).

### 8. Boundary of what this schema can honestly claim

The investigator today lets the model Glob/Grep/Read arbitrarily inside its
cloned tree; a faithful per-file source list cannot be produced without a
tool-call hook that does not exist. The contract's answer: a single
`target-repo` source pinned by `target_repository_sha` with
`selector.mode: agent-directed` is contractually valid and honest. Per-file
enumeration is an optional refinement a later retrieval implementation may
add without a schema change — see follow-up (e) below.

## Alternatives

1. **Extend `ExecutionPlan` with context fields instead of a new type.**
   Rejected: `ExecutionPlan` is resolved *before* submission from committed
   files and pinned by publish-time hashes; context is gathered *during*
   execution from mutable external systems. Merging them would put a
   runtime-varying, per-step, possibly-large structure inside the one object
   ADR 007 defines as "created once and never follows later promotions"
   (`docs/adr/007-...:222`), and would force plan re-materialization per
   step. `ContextSnapshot` instead *quotes* the plan's pins in its
   `execution` block, preserving one-way reference.
2. **Store retrieved payloads in the snapshot (or in traces) for full
   replayability.** Rejected: the issue forbids it, and the repo's own
   hardening agrees — issue bodies are untrusted text, Loki tails routinely
   carry tokens and PII, and `state.py`/`registry.py` payloads already
   travel to mctl-api over the network without needing a second copy. Hash +
   locator + selector + byte count gives auditability and correlation at a
   fraction of the blast radius; verification requires re-fetching the
   source, and a mutated source is detectable (hash mismatch) but not
   recoverable — an honest cost, not a hidden one.
3. **Hash pre-redaction bytes so the "true" source is identified.**
   Rejected: it would make `content_hash` unverifiable by anyone holding
   only the snapshot (the raw bytes are, by design, never stored), and it
   decouples the hash from what the model actually saw — the thing a reader
   of a bad proposal needs to reconstruct. `redaction.applied`/`rules`/
   `dropped_bytes` preserve the fact that removal occurred without needing
   the removed bytes.
4. **Model context selection as a policy input ("only surface what the
   profile permits, and treat surfacing as permission").** Rejected and
   named as the anti-goal: it would make a ranker a privilege-escalation
   surface and put an LLM-influenced score on the authorization path.
   Eligibility stays in `ExecutionProfile`/#242; enforcement stays with #197
   and the providers, exactly as ADR 007 already rules for `tools`
   (`docs/adr/007-...:106-110`).

## Non-goals

- Implementing semantic or vector retrieval, embeddings, or any ranking
  algorithm. This ADR defines where a ranker's identity is recorded, not
  what a ranker does.
- Building a generic search or context service, or any new HTTP API.
- Changing issue-investigator, implementer, shepherd, incident-responder,
  service-agent or mentor behaviour, prompts, tools or budgets. No agent
  produces a snapshot as part of this proposal.
- Implementing #195 traces, #197 policy checkpoints, #199 evidence or #242
  capability discovery/invocation. This ADR fixes the seams they plug into,
  as ADR 007 did for its own follow-ups.
- Storing full logs, prompts, diffs or secrets anywhere in a snapshot.
- Any mctl-api schema migration or new registry table. Persistence is
  specified as a contract; wiring it into mctl-api is a named follow-up
  (sec. "Follow-ups and sequencing" below).
- Token counting, model-context-window accounting, or cost attribution.

## Platform impact

- **Migrations:** none. No mctl-api table, no GitOps schema, no manifest
  field changes. `agents/_manifests/*/agent.yaml`, `orchestrator/resolver.py`
  and every workflow are untouched. `orchestrator/context_snapshot.py` is
  imported by tests and fixture generation only until a follow-up wires a
  producer.
- **Backward compatibility:** additive by construction. Optional fields
  carry dataclass defaults, the same pattern `dev_loop.py:240,246` and
  `pr_state.py`'s `PRState` already rely on for Temporal payload evolution.
  `apiVersion` bumps only for a breaking change; `strategy.version` and
  `ranker.version` absorb configuration changes without one. An unknown
  `apiVersion`/`kind` fails loudly, mirroring `orchestrator/manifest.py`'s
  `SUPPORTED_API_VERSIONS` allow-list (`manifest.py:35`) and its `load()`
  check (`manifest.py:135-143`) — never a silent fallback to a default
  shape.
- **Resource impact:** negligible. A sealed snapshot is a few kilobytes of
  primitives; one sha256 over that JSON per seal. Because payloads are
  excluded, snapshot size is bounded by source *count*, not source *size*.
  Stdlib-only imports keep this importable by the 256Mi Temporal worker
  (ADR 008 context, `docs/adr/008-worker-queue-split-and-capacity.md`)
  without pulling in the agent stack.
- **Risks and mitigations:**
  - *The contract is written and then ignored by the retrieval follow-up.*
    Mitigated as ADR 007 mitigated it: this ADR names which decisions are
    normative and may not be reopened, and the schema module plus the
    checked-in fixture are the executable form of those decisions.
  - *Snapshots become a covert evidence or payload store.* Mitigated by
    `from_dict` rejecting unknown keys, bounded `locator`/`selector` length,
    and `tests/test_context_snapshot.py` asserting both.
  - *Relevance drifts into authorization.* Mitigated by the field-name and
    import-direction tests (sec. 5), plus this ADR's non-negotiable
    sentence.
  - *Hash instability makes ids meaningless.* Mitigated by excluding
    `created_at` from the hash, canonical `sort_keys` JSON, and a golden
    fixture whose hash is asserted byte-for-byte
    (`tests/fixtures/context/investigator-snapshot.json`).
  - *A second, disagreeing hash convention creeps in.* Two prompt-hash
    algorithms already disagree in this repository
    (`tools/publish_agent_release.py` length-prefixes its blobs;
    `resolver._hash_prompt_source`, `resolver.py:722-753`, does not); this
    ADR does not attempt to reconcile them, and defines exactly one
    canonical-JSON hash rule for `ContextSnapshot` (sec. 2).
- **Security:** the snapshot is a strictly non-authoritative, payload-free
  description. Provider-side authorization (GitHub, mctl MCP, Kubernetes,
  Loki) remains the only enforcement, unchanged by this ADR.

## Follow-ups and sequencing

None of the following is a prerequisite for merging this proposal — the ADR
and schema module are the complete deliverable, and every item below is
future work this ADR fixes the seam for, not work it performs:

| Follow-up | Owning issue |
|---|---|
| (a) A producer wired into `run_issue_investigator.py` that calls `seal()` with real fetched/hashed sources | needs an issue |
| (b) Persisting sealed snapshots next to `ExecutionRecord` in mctl-api (the "Where sealed snapshots durably live" open question — `retention: execution-record` names the intended home) | needs an issue |
| (c) A redaction helper that hashes post-redaction bytes for real (sec. 7 states none exists today) | needs an issue |
| (d) Emitting `to_log_dict()`'s attributes into #195 traces | mctlhq/mctl-agents#195 |
| (e) Per-file enumeration of agent-directed reads inside `target-repo` sources, replacing the single `selector.mode: agent-directed` source with a real per-file list (sec. 8) | needs an issue |

## Implementation map

This PR changes only:

```
docs/adr/009-context-snapshot-contract.md
docs/adr/007-agent-definition-execution-profile-contract.md  # one cross-link line
docs/agent-inventory.yaml                                    # one cross-link line
orchestrator/context_snapshot.py                              # new module
tests/test_context_snapshot.py                                # new tests
tests/fixtures/context/investigator-snapshot.json             # new fixture
```

The contract is normative for every follow-up in the table above. A
follow-up may add producer/persistence/redaction/trace-emission
implementation detail, but must not reopen: the field shape and owner table
(sec. 1); the `content_hash`/`snapshot_id` derivation (sec. 2); the
root/step lifecycle and the equal-`execution`-block chaining rule (sec. 3);
the boundary table and the "context relevance is never authorization"
sentence (sec. 5); the closed budget/freshness/trust vocabularies and the
token-budget deferral (sec. 6); or the hash-after-redaction, no-payload,
bounded-length retention rules (sec. 7).
