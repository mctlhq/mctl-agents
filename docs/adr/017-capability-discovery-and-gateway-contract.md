# ADR 017 — Capability discovery and MCP gateway contract

> **Status:** accepted, slice 1 of 3 (contract only — see "Implementation
> map")
> **Date:** 2026-09-24
> **Supersedes:** nothing — this is a design document; the code that ships
> with slice 1 is one new, inert, additive schema module plus a checked-in
> classification table

## Context

ADR 007 (`docs/adr/007-agent-definition-execution-profile-contract.md`) and
the `#227` resolver pilot (`orchestrator/resolver.py`) already answer "what
contract ran": `execute(agent, task)` materializes one immutable
`ExecutionPlan` carrying `tools`, `permissions`, `policy_ref`, budget,
timeout and sandbox. What no part of this repository answers is **what the
model could actually see and call**. Every non-shepherd options builder in
`orchestrator/options.py` connects the remote mctl MCP server
(`mctl_mcp_config(always_load=True)`) and grants the single wildcard
`mcp__mctl__*` (`_mctl_tool_globs()`, `options.py:57`). The wildcard is a
*permission* filter, not an *exposure* filter: the CLI connects the server
and loads every advertised tool schema into the first turn regardless of how
narrow the allow-list is. `docs/diagrams/archify/facts.yaml`'s checked-in
`mcp_tools` snapshot (generated from mctl-api's `internal/mcp/server.go` by
`tools/diagram_facts.py`) names 73 tools today — deploy, retire, tenant
delete, domain, skill and agent-promotion tools among them — so every
investigator run pays for the full platform surface before it has read one
line of the issue.

ADR 009 (`docs/adr/009-context-snapshot-contract.md`) already wrote down the
invariant this proposal exists to make testable, and named this issue by
number in its boundary table (sec. 5): a `ContextSnapshot`'s capability-
eligibility row belongs to `ExecutionProfile.tools` / `orchestrator/
resolver.py`, and **mctlhq/mctl-agents#242** — this ADR — owns turning that
eligibility into an exposure contract. ADR 009's own non-goals say plainly
that `#242` capability discovery is not implemented by that ADR. `#195`
traces, `#196` execution identity and `#197` policy checkpoints have partial
implementations elsewhere in this repository today
(`orchestrator/execution_identity.py`, `orchestrator/policy_checkpoint.py`,
ADR 011, ADR 014) but none of them yet govern an MCP capability invocation;
this ADR fixes the seam they plug into for that purpose, the way ADR 007
fixed the seam `#227`'s resolver was built against.

This issue is architecture-first, delivered in three slices (`requirements.md`
"User stories", `tasks.md` revision 2, dated 2026-09-23):

1. **This ADR, plus `orchestrator/capability.py` and
   `config/capability-consequence.yaml`** — the descriptor shape, the sealed
   `CapabilitySet` contract, the `PolicyCheckpoint` seam, and the consequence
   classification table. Additive and unwired: no builder, no prompt, no
   catalog field and no worker path changes.
2. **`orchestrator/capability_gateway.py`** — `resolve_eligible`, the three
   `capability_search`/`capability_describe`/`capability_invoke` SDK tools,
   `#196` metadata propagation and `#195`-shaped tracing.
3. **Builder/driver wiring** — `build_issue_investigator_options_from_plan(...,
   gateway=None)`, `ISSUE_INVESTIGATOR_CAPABILITY_MODE`, the mctl-gitops
   `spec.capabilityDiscovery` catalog field, mode-aware `validate_manifest.py`,
   and the `tools/capability_bench.py` benchmark.

Every decision below is normative for all three slices; only slice 1 ships
code with this ADR.

## Decision

### 1. Canonical shape — `capability.mctl.ai/v1alpha1`, `kind: CapabilitySet`

A `CapabilitySet` is a frozen, JSON-primitives-only document, modelled
field-for-field on `orchestrator/context_snapshot.py`'s `ContextSnapshot`:
frozen dataclasses, a `sha256:`-prefixed content hash, `seal()` as the only
constructor that fills identity, `from_dict` that rejects unknown keys, and
bounded string lengths. Implemented in `orchestrator/capability.py`.

**`CapabilitySet`**

| Field | Type | Owner | Meaning |
|---|---|---|---|
| `api_version` | str | this contract | always `capability.mctl.ai/v1alpha1` |
| `kind` | str | this contract | always `CapabilitySet` |
| `capability_set_id` | str | `seal()` | `"cap-" + content_hash[7:23]`, derived not random |
| `content_hash` | str | `seal()` | `"sha256:..."` over every other field except `created_at` |
| `created_at` | str (ISO-8601) | caller | assembly time; excluded from the hash |
| `execution` | `ExecutionCorrelation` | caller (from `ExecutionPlan` + workflow identity) | joins the set to the run that produced it — **the same type ADR 009 defines**, imported from `orchestrator/context_snapshot.py`, not re-derived, so a `ContextSnapshot` and a `CapabilitySet` sealed for one execution always join on identical bytes |
| `plan_tools` | str[] | caller | the verbatim `ExecutionPlan.tools` this set was narrowed from |
| `providers` | `ProviderRef[]` | caller / gateway (slice 2) | every provider consulted |
| `capabilities` | `CapabilityDescriptor[]` | gateway (slice 2) | every capability that matched `plan_tools` |
| `excluded_count` | int | gateway (slice 2) | how many advertised capabilities matched nothing — a count, never their names |
| `strategy` | `CapabilityStrategy` | gateway/ranker (slice 2) | which discovery/ranking logic produced this set |
| `retention` | `RetentionPolicy` | assembler + the store that persists it | which store honours this set and for how long |

**`ProviderRef`** — `type: "mcp-remote" | "mcp-local" | "sdk-builtin"`,
`id: str`, `alias: str`, `endpoint_ref: str`. `alias` is execution-scoped and
assigned deterministically from the profile's provider declaration order
(slice 2's job); this ADR fixes the shape.

**`CapabilityDescriptor`** — the canonical shape shared by every provider,
remote or execution-local; only `provider.type` distinguishes them:

| Field | Type | Meaning |
|---|---|---|
| `capability_id` | str | canonical `mctl://<provider_type>/<provider_id>/<tool>` — stable across alias reassignment |
| `tool_name` | str | the SDK-visible name: `mcp__<alias>__<tool>` for a remote/local MCP tool, or a built-in tool name |
| `provider` | `ProviderRef` | which provider advertised it |
| `title` | str (bounded) | short label |
| `summary` | str (bounded) | one-line, search-index text |
| `keywords` | str[] (bounded count/length) | search-index terms |
| `input_schema_hash` | str | `sha256:...` of the tool's input JSON Schema |
| `input_schema_bytes` | int | size of that schema — never the schema itself; this is a descriptor, not a payload carrier |
| `consequence` | str | closed vocabulary: `read-only \| mutating \| consequential` (sec. 8) |
| `matched_tool_pattern` | str | the exact `ExecutionPlan.tools` entry that made this capability eligible — the field the narrowing invariant (sec. 3) checks |
| `annotations` | object (bounded) | advisory MCP `ToolAnnotations` (`readOnlyHint`/`destructiveHint`), corroborating only, never authoritative (sec. 8) |

**`CapabilityStrategy`** — `name: str`, `version: str`, `ranker_name: str |
null`, `ranker_version: str | null` — mirrors ADR 009 sec. 1's
`ContextStrategy`; no ranker beyond a lexical pilot exists yet (slice 2).

**`RetentionPolicy`** — `class: "telemetry" | "execution-record" | "gitops"`,
`expires_after_days: int` — the same three-value vocabulary and open
question ADR 009 left for `ContextSnapshot`
(requirements.md "Where a sealed CapabilitySet durably lives"), answered the
same way: structured logs plus this field now, mctl-api persistence tracked
separately.

**`DiscoveryDecision`** (slice 2 producer) — `capability_id`, `rank`,
`score: float | null`, `reason_code`, `included` — one capability's place in
one `capability_search` result. Never a title, summary or schema; those live
on `CapabilityDescriptor`.

**`InvocationRecord`** (slice 2 producer) — `capability_id`,
`capability_set_id`, `outcome`, `reason_code`, `duration_ms`,
`policy_checkpoint: "absent" | "allowed" | "denied"`, `arguments_hash`,
`result_hash` — hashes only, never the arguments or the result;
`from_dict`'s unknown-key rejection is what keeps a payload field from ever
being smuggled onto a record of this kind.

No field in this shape may hold a source payload, an argument, or a result:
there is no such field declared anywhere in it, and every `from_dict` in
`orchestrator/capability.py` rejects a key it does not know about.

### 2. Identity and immutability

`content_hash = "sha256:" + sha256(canonical JSON of every field except
content_hash, capability_set_id, created_at)`, using
`json.dumps(payload, sort_keys=True, separators=(",", ":"))` — the identical
convention ADR 009 sec. 2 fixed for `ContextSnapshot`, reused here via
`orchestrator/context_snapshot.py`'s public `canonical_json`/`hash_bytes`
helpers rather than a second, disagreeing implementation.
`capability_set_id = "cap-" + content_hash[7:23]` — derived from content,
never random. `seal()` is the only constructor that fills `content_hash` and
`capability_set_id`; there is no mutator. `created_at` is excluded from the
hash, so sealing the same logical input twice at different wall-clock times
yields the same identity — the DoD `tasks.md` names explicitly for slice 1.

### 3. The narrowing invariant

**Discovery narrows, it never grants** — ADR 009 sec. 5's rule, restated
here in the direction this ADR owns. `CapabilitySet.validate()` makes it a
checked property of every sealed document, not a convention a caller must
remember:

- Every member's `matched_tool_pattern` MUST be a verbatim element of
  `plan_tools` (`ExecutionPlan.tools`) — a capability whose eligibility
  cannot be pointed at a plan entry was never eligible.
- Every member's `tool_name` MUST match its own `matched_tool_pattern` under
  `fnmatch.fnmatchcase` — the same case-sensitive semantics
  `orchestrator/policy_checkpoint.py`'s `evaluate()` already uses for its own
  operation-pattern matching, so "matches a pattern" means one thing across
  this codebase.

A capability failing either check can never have been sealed as eligible: a
ranker (slice 2) may record that a capability exists and order it, but it
may never widen `ExecutionPlan.tools`, and satisfying this invariant is a
shape-level proof, not a policy decision — matching ADR 009 sec. 5's own
"context relevance is never an authorization mechanism."

### 4. Namespacing and collision rules (normative for slice 2)

`provider.alias` is execution-scoped and assigned deterministically from the
profile's provider declaration order — the mctl-gitops `ExecutionProfile` is
the assigning authority (`requirements.md` open question "Which authority
assigns `provider_id`"), because that is where every other execution-
affecting input is already reviewed. `capability_id` is the fully-qualified
`mctl://<provider_type>/<provider_id>/<tool>` URI, stable even if an alias is
reassigned between runs; the SDK-visible name stays `mcp__<alias>__<tool>`,
so nothing about the CLI's own naming rules changes. Two providers resolving
to the same SDK-visible `tool_name`, or two providers claiming one alias, is
a **`collision`** error raised at set-sealing time (slice 2's
`resolve_eligible`) — fail closed, never a silent rename, shadow or drop.
`collision` is in this ADR's closed reason-code vocabulary (sec. 7) for
exactly this reason, even though slice 1 does not yet implement the
detection that raises it.

### 5. Gateway routing, identity and failure semantics (normative for slice 2)

`orchestrator/capability_gateway.py` is the runtime this ADR's contract
serves, imported lazily inside `_run_agent` exactly like `resolver`/`options`
already are, so worker isolation holds
(`tests/test_worker_isolation.py`, sec. 9 below).

- `resolve_eligible(plan, correlation, providers) -> CapabilitySet` connects
  each declared provider once, lists its tools, matches advertised names
  against `plan.tools` with `fnmatch`, classifies consequence from the
  checked-in table (sec. 8), and seals. A provider that fails to list raises
  `provider-unavailable` rather than sealing a smaller set — the
  `orchestrator/mcp_guard.py` lesson restated at set level: a short set and a
  broken provider must never look alike.
- The gateway exposes exactly three SDK tools via `create_sdk_mcp_server`:
  `capability_search` (compact rows — id, title, one-line summary,
  `consequence` — never schemas, drawn only from the sealed set),
  `capability_describe` (full input schemas for at most `MAX_DESCRIBE_IDS`
  eligible ids; `not-found` for anything outside the set — indistinguishable
  from a nonexistent id, so discovery leaks nothing about what was withheld),
  and `capability_invoke` (membership check against the sealed set, then the
  `PolicyCheckpoint` sec. 6 defines, then dispatch).
- **Identity propagation (`#196`).** Every remote invocation carries the
  execution correlation fields (`agent`, `environment`,
  `temporal_workflow_id`, `argo_workflow_name`, `target_repository_sha`,
  definition/profile versions, `capability_set_id`) as provider request
  metadata alongside the existing `MCTL_TOKEN` bearer credential. The
  credential remains what the provider authorizes on; the metadata is for
  correlation only, so a future reader cannot mistake it for an
  authorization claim.
- **Execution-local dispatch.** A capability whose `provider.type` is
  `mcp-local` or `sdk-builtin` dispatches in process, with no network hop,
  while being described by the identical `CapabilityDescriptor` shape a
  remote capability uses.
- **Tracing (`#195`).** One `to_log_dict()` line per sealed set and per
  invocation — ids, hashes, counts, durations, reason codes. No arguments, no
  results, no string derived from either, matching ADR 009 sec. 5's trace
  row.

### 6. The `PolicyCheckpoint` seam (`#197`)

`orchestrator/capability.py` defines a narrow, synchronous
`typing.Protocol`: `check(descriptor: CapabilityDescriptor, correlation:
ExecutionCorrelation) -> CheckpointVerdict`
(`CheckpointVerdict.decision: "allowed" | "denied"`), answering the
requirements.md open question "Whether `#197` will expose a synchronous
in-process check or an out-of-process call" by giving the invocation path
one shape regardless of which lands. `AbsentPolicyCheckpoint` is the named,
logged pass-through adapter used while no real checkpoint is wired: it
always answers `allowed`, and the resulting `InvocationRecord` records
`policy_checkpoint: "absent"` — via `policy_checkpoint_status()`, a pure
helper keyed on the checkpoint instance, never `"allowed"`, which would
misrepresent a real decision having been made. Swapping in a real `#197`
checkpoint touches exactly one construction site — whichever call currently
builds `AbsentPolicyCheckpoint()` builds the real adapter instead; nothing
about `InvocationRecord`'s shape, `reason_code_for_verdict()`, or
`policy_checkpoint_status()` changes.

This seam is deliberately narrower than `orchestrator/policy_checkpoint.py`
(ADR 014, `#197`'s general `ActionRequest -> decide()` decision point for
every consequential orchestrator-driven action, already landed and in use
for GitHub mutations and mctl work-item writes). This ADR's
`PolicyCheckpoint` is the capability-invocation-specific instantiation of
that same seam — slice 2's `capability_invoke` is expected to call into the
general checkpoint (via a `PolicyCheckpoint` adapter that wraps
`orchestrator.policy_checkpoint.decide`) rather than reinvent policy
evaluation, but that wiring is slice 2's job; this ADR fixes the shape the
adapter must satisfy, not the wiring.

### 7. Closed reason-code vocabulary

`ok`, `not-eligible`, `not-found`, `policy-denied`, `invalid-arguments`,
`provider-unavailable`, `provider-error`, `timeout`, `collision`. Shared
between `DiscoveryDecision.reason_code` and `InvocationRecord.reason_code`.
A provider outage, an error, and a timeout are three distinct codes, never
collapsed into an empty-but-successful discovery — the same rule
`orchestrator/mcp_guard.py` already enforces for eager-mode connectivity,
restated for discovery mode.

### 8. Consequence classification

MCP `ToolAnnotations` (`readOnlyHint`/`destructiveHint`) are advisory and not
guaranteed present on mctl-api's tools (requirements.md open question
"Consequence classification source"). The authoritative source is a
checked-in table, `config/capability-consequence.yaml`, mapping every
tool name in `docs/diagrams/archify/facts.yaml`'s `mcp_tools` snapshot (the
same mctl-api `server.go`-derived inventory `tools/diagram_facts.py`
generates) to `read-only | mutating | consequential`. Loaded and applied by
`orchestrator.capability.load_consequence_table()` /
`classify_consequence()`, which accepts either the bare mctl-api tool name or
the SDK-visible `mcp__<alias>__<tool>` spelling. **Any tool absent from the
table classifies `consequential`** — the fail-safe default, hard-coded in
the loader rather than configurable from the file itself, so the table can
only narrow which tools skip the checkpoint, never widen it by omission. A
`read-only` capability may still go through `capability_search`/`describe`
freely; `mutating` and `consequential` capabilities are the ones
`capability_invoke` (slice 2) submits to the `PolicyCheckpoint` before
dispatch.

### 9. Boundary rules — normative and testable (mirrors ADR 009 sec. 5)

| Concern | Owner | What `CapabilitySet` may record | What it must never do |
|---|---|---|---|
| Capability eligibility | `ExecutionProfile.tools` / `orchestrator/resolver.py`'s `ExecutionPlan` | Which `ExecutionPlan.tools` entry made a capability eligible (`matched_tool_pattern`) | Widen eligibility; discovery may record that a capability exists and rank it (`DiscoveryDecision`), but it may never add a member `ExecutionPlan.tools` does not already cover, and `validate()`'s narrowing invariant (sec. 3) makes that a checked property |
| Authorization (`#197`) | `PolicyCheckpoint` (sec. 6) + provider-side enforcement | `policy_checkpoint: absent \| allowed \| denied` on an `InvocationRecord` — a status, never a decision this module makes itself | No allow/deny/permit/grant/authorized field exists anywhere in the schema (`tests/test_capability.py`'s field-name assertion enforces this); no policy-decision path may import `orchestrator/capability.py` to fabricate a verdict |
| Consequence classification | `config/capability-consequence.yaml` + `classify_consequence()` | A closed `read-only \| mutating \| consequential` tier per capability | Default an unclassified tool to anything but `consequential`; a misclassification is fail-safe by construction, not by review alone |
| Traces (`#195`) | Trace/telemetry pipeline | `capability_set_id`, `content_hash`, strategy name+version, counts (`to_log_dict()`) | Emit a `tool_name`, `title`, `summary`, `locator`, or any string derived from a schema or an argument/result |

**Discovery narrows, it never grants; ranking is not authorization.** Two
tests make this executable rather than aspirational
(`tests/test_capability.py`, mirroring `tests/test_context_snapshot.py`'s T5/
T8): a recursive field-name assertion that no
`allow`/`deny`/`permit`/`grant`/`authorized` token appears anywhere in the
serialized schema or any dataclass field name, and a subprocess
import-direction assertion (the style of `tests/test_worker_isolation.py`)
proving `orchestrator/capability.py` loads stdlib only.

## Alternatives

1. **Filter server-side: have mctl-api advertise a per-role tool subset.**
   Genuinely cheaper in context and arguably the right long-term home, but it
   makes the MCP server the owner of a decision ADR 007 assigns to the
   reviewed `ExecutionProfile`, gives execution-local tools no path into the
   same descriptor, and requires a cross-repo API change before anything can
   be measured here. Dropped as the primary mechanism, kept as a
   complementary optimisation the gateway can consume unchanged once it
   exists.
2. **Enumerate explicit tool names in the profile instead of `mcp__mctl__*`.**
   Cheap and static, but does not solve the stated problem: the server is
   still connected wholesale and every schema still enters context, because
   `allowed_tools` filters permission, not exposure. Dropped as insufficient;
   the narrowing it offers is subsumed by `plan.tools` matching in
   `resolve_eligible`.
3. **Deploy a standalone MCP aggregator/proxy service in-cluster.** The
   textbook gateway shape, and the right answer once several unrelated
   remote servers exist. Dropped for this pilot: it adds a deployment, a
   second identity hop and a new failure domain; it cannot host
   execution-local tools without violating the issue's own non-goal about
   proxying local filesystem/git operations through a remote server; and
   mctl governance (profile eligibility, `#197` checkpoints, correlation)
   would have to be reimplemented server-side. The in-process gateway reuses
   the existing process identity and the existing connectivity-verification
   model; this ADR keeps the contract transport-agnostic so the gateway can
   be lifted out later without changing descriptors.
4. **Ship discovery with no policy seam and let `#197` retrofit it.**
   Dropped: an invocation path built without a checkpoint tends to grow
   callers that assume none exists, and the pilot would then be the thing
   `#197` has to unpick. A one-method protocol plus an explicitly named
   `AbsentPolicyCheckpoint` costs almost nothing and makes the absence
   visible on every invocation record.

## Non-goals

- Implementing semantic or vector retrieval, embeddings, or any ranking
  algorithm. This ADR defines where a ranker's identity is recorded
  (`CapabilityStrategy`), not what a ranker does.
- Building a generic search or context service, or any new HTTP API.
- Replacing Temporal or Argo, or changing any workflow topology.
- A public or general-purpose MCP marketplace, registry UI, or catalog of
  third-party servers.
- Treating capability discovery, ranking or relevance as the security
  boundary; provider-side authorization stays authoritative (sec. 9).
- Proxying local filesystem, git, or CLI operations (`Read`, `Glob`, `Grep`,
  `Bash`, `Write`, `Edit`) through a remote MCP server.
- Implementing `#197`'s general policy-decision engine (already landed
  separately as `orchestrator/policy_checkpoint.py`, ADR 014), `#195`'s
  trace pipeline, `#196`'s execution-identity store, or `#199`'s evidence
  store. This ADR defines and consumes their seams.
- Changing investigator (or any other agent's) product behaviour: the
  prompt's task, the proposal triplet, staging/publish rules and budget are
  untouched by slice 1, and remain untouched by slices 2/3 beyond capability
  loading mechanics.
- Migrating `implementer`, `shepherd`, `service-agent`, `mentor` or
  `incident-responder` to discovery mode, or flipping any agent's default
  mode.
- **Slice 1 specifically does not implement**: `orchestrator/
  capability_gateway.py`, the three gateway SDK tools, `#196` metadata
  propagation, `#195` trace emission, `ISSUE_INVESTIGATOR_CAPABILITY_MODE`,
  any change to `orchestrator/options.py` / `orchestrator/
  run_issue_investigator.py` / `orchestrator/validate_manifest.py`, the
  mctl-gitops `spec.capabilityDiscovery` catalog field, or
  `tools/capability_bench.py`. Slice 1 ships a contract module that nothing
  imports yet.

## Platform impact

- **Migrations:** none in this repo's data for slice 1. Slice 3 adds one
  additive, defaulted field to the mctl-gitops `ExecutionProfile` schema
  (`spec.capabilityDiscovery`); no mctl-api schema change is needed for any
  slice.
- **Backward compatibility:** slice 1 is purely additive — new files only,
  nothing imports `orchestrator/capability.py`. When slice 3 lands, default
  `eager` mode keeps `build_issue_investigator_options*` byte-identical to
  today, and every other agent (`implementer`, `shepherd`, `service-agent`,
  `mentor`, `incident-responder`) is untouched by any slice of this ADR.
- **Cross-repo lockstep:** the main sequencing risk, inherited from the
  `validate_manifest` equality check ADR 009's own follow-up already names
  (`docs/resolver-pilot-status.md` records exactly this coupling turning
  `tests/test_manifest.py` red on main once already). Mitigation: mode-aware
  validation (slice 3) lands and is tested against a fixture *before* any
  mctl-gitops catalog edit, so the eager comparison is provably unchanged
  when the field appears.
- **Dependency:** slice 1 adds none — `orchestrator/capability.py` stays
  stdlib-only, and its one YAML-reading function
  (`load_consequence_table`) imports `yaml` lazily inside the function body,
  not at module scope, so PyYAML (already a direct pinned dependency) is
  pulled in only when that function is actually called. Slice 2 promotes
  `mcp` from transitive to a direct pinned dependency, following the
  precedent set for `httpx`.
- **Worker isolation:** `orchestrator/capability.py` is stdlib-only and
  importable by the 256Mi Temporal worker (ADR 008);
  `orchestrator/capability_gateway.py` (slice 2) will import the SDK and the
  `mcp` client and must only be imported inside `_run_agent`.
  `tests/test_worker_isolation.py` is extended in slice 2 to assert both
  halves of that line.
- **Resource impact:** slice 1 has none at runtime — the module is dead code
  until slice 2 wires it in. Slice 2's expected cost: one `list_tools` round
  trip per execution (cached for the run) plus one sha256 per descriptor.
- **Risks and mitigations:**
  - *The contract is written and then ignored by the gateway follow-up.*
    Mitigated as ADR 007/009 mitigated it: this ADR names which decisions
    are normative and may not be reopened (sec. 3, 6, 7, 8, 9), and the
    schema module plus its golden fixture are the executable form of those
    decisions.
  - *Discovery drifts into authorization.* Mitigated by the field-name and
    import-direction tests (sec. 9), the narrowing-invariant test, and this
    ADR's non-negotiable sentence.
  - *A capability is misclassified as `read-only` and skips the checkpoint.*
    Unclassified defaults to `consequential` (sec. 8); the classification
    table is checked in, reviewed, and covered by a test asserting every
    tool in the repo's own checked-in mctl tool inventory
    (`docs/diagrams/archify/facts.yaml`) has an entry.
  - *A second, disagreeing hash convention creeps in.* Avoided entirely for
    slice 1 by reusing `orchestrator/context_snapshot.py`'s
    `canonical_json`/`hash_bytes` rather than reimplementing them.
- **Security:** the eligible set can only shrink relative to
  `ExecutionPlan.tools` (sec. 3); provider-side authorization (mctl-api,
  GitHub, Kubernetes) remains the only enforcement, unchanged by this ADR.
  The gateway (slice 2) holds the same `MCTL_TOKEN` the CLI holds today — not
  a new credential — and descriptors never carry credentials, arguments or
  results.

## Implementation map

This PR (slice 1) changes only:

```
docs/adr/017-capability-discovery-and-gateway-contract.md
docs/adr/009-context-snapshot-contract.md      # one cross-link line
docs/agent-inventory.yaml                      # one cross-link line
orchestrator/capability.py                     # new module
config/capability-consequence.yaml             # new classification table
tests/test_capability.py                       # new tests (T1, T2, T4, T8, T15)
tests/fixtures/capability/investigator-capability-set.json  # new fixture
```

The contract is normative for `#197`, `#195` and `#196` wherever they touch
capability invocation, and for slices 2 and 3 of this same issue. A
follow-up may add gateway/builder/catalog implementation detail, but must
not reopen: the field shape and owner tables (sec. 1); the
`content_hash`/`capability_set_id` derivation (sec. 2); the narrowing
invariant (sec. 3); the namespacing/collision rule (sec. 4); the gateway's
routing/identity/failure semantics (sec. 5); the `PolicyCheckpoint` seam and
the meaning of `policy_checkpoint: absent` (sec. 6); the closed reason-code
vocabulary (sec. 7); the consequence-classification fail-safe default
(sec. 8); or the boundary table and "discovery narrows, it never grants"
sentence (sec. 9).

### Downstream sequencing

- Slice 2 (`orchestrator/capability_gateway.py` and its gateway tools) may
  begin once slice 1 has merged; it depends on `orchestrator/capability.py`'s
  `seal()`, `validate()`, `PolicyCheckpoint` and consequence loader exactly
  as specified here.
- Slice 3 (builder/driver wiring, the mctl-gitops catalog field, mode-aware
  `validate_manifest.py`, the benchmark) depends on slice 2, and — per the
  sequencing note `tasks.md` carries forward from revision 1 — the
  mode-aware `validate_manifest.py` change must land before the mctl-gitops
  `spec.capabilityDiscovery` field is added to any profile, so the
  cross-repo equality check can never go red on `main` in either repository.
