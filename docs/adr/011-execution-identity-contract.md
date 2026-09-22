# ADR 011 — `ExecutionContext` execution-identity contract

> **Status:** accepted
> **Date:** 2026-09-19
> **Issue:** mctlhq/mctl-agents#196
> **Supersedes:** nothing — this is a design document; the code that ships
> with it is a new, inert, additive schema module plus the driver-side
> wiring named in the Implementation map below

## Context

Every governance capability the platform is building — execution traces
(#195), runtime policy checkpoints (#197), human approval (#198), execution
evidence (#199) — needs one prior thing that does not exist yet: a
well-defined answer to *who and what is executing, on whose behalf, in which
scope*. Today that answer is scattered and lossy:

- The Temporal control plane knows the workflow id
  (`orchestrator/temporal/issue_ref.py:30`,
  `dev-loop-mctlhq-<repo>-<number>`) and, after an approve signal, an
  approver string (`orchestrator/temporal/workflows/dev_loop.py:787-800`) —
  falling back to the literal `"unknown"` when the approve signal carries no
  payload (`dev_loop.py:897-905`).
- The boundary into Argo carries a flat `dict[str, str]` of at most seven
  keys (`SubmitAndWaitInput.params`,
  `orchestrator/temporal/activities/argo.py:61-67`): `issue_url`, `service`,
  `slug`, `approver`, `agent_image`, `agent_version`, `mode`.
- The durable ledger (`ExecutionRecord`,
  `orchestrator/temporal/activities/state.py:28-44`) records eight fields —
  no run id, no attempt, no approver, no content hash.
- The agent container reconstructs whatever it needs from argv and
  environment, and every `mcp__mctl__*` call it makes reaches mctl-api under
  one static shared bearer token (`orchestrator/temporal/mctl_client.py:16-31`,
  `orchestrator/options.py`) that names no run, no agent, no actor and no
  environment.

Two contracts already anticipate this work and constrain its shape. **ADR
009** (`docs/adr/009-context-snapshot-contract.md`) defines `ContextSnapshot`
and an `ExecutionCorrelation` block
(`orchestrator/context_snapshot.py:407-475`) that is explicitly a copy
supplied by the caller, not an assertion, and forbids any authorization-
shaped field. **ADR 007** separates `AgentDefinition` (identity) from
`ExecutionProfile` (constraints) and states that `tools` is not
authorization. **ADR 010** already names actors without naming permissions:
`Executor{type, id}` (`orchestrator/lifecycle/contract.py:670`) with the
closed vocabulary `shepherd | pr-steward | devloop-workflow | reconciler |
implementer`.

This ADR defines the canonical, versioned `ExecutionContext` document and
the rules for minting, propagating, consuming and trusting it. It answers
*who is executing*; it never answers *what they may do* — that stays with
#197.

## Decision

### 1. Canonical shape — `identity.mctl.ai/v1alpha1`, `kind: ExecutionContext`

Implemented in `orchestrator/execution_identity.py`, a stdlib-only module
mirroring `orchestrator/context_snapshot.py` line for line in style: module
constants `API_VERSION`/`KIND`/`SUPPORTED_API_VERSIONS`, closed `frozenset`
vocabularies, frozen dataclasses with `to_dict`/`from_dict`,
`_require_str`/`_require_int`/`_reject_unknown_keys` validators, one
`ExecutionIdentityError(ValueError)`, a keyword-only `seal()` and a
`to_log_dict()`.

| Field | Type | Owner | Meaning |
|---|---|---|---|
| `api_version` | str | this contract | always `identity.mctl.ai/v1alpha1` |
| `kind` | str | this contract | always `ExecutionContext` |
| `context_id` | str | `seal()` | `"ex-" + content_hash[7:23]`, derived not random |
| `content_hash` | str | `seal()` | `"sha256:..."` over every field except `content_hash`, `context_id`, `issued_at` |
| `issued_at` | str (RFC 3339 `Z`) | caller | mint time; excluded from the hash |
| `trace_id` | str | caller (control plane, once wired) | 32 lowercase hex (W3C trace-context), stable for one DevLoop execution |
| `parent_context_id` | str \| null | caller | `null` for a root context; set for a per-step child |
| `workflow_type` | str | caller | closed vocabulary, sec. below |
| `step_sequence` | int | caller | strictly increasing among children of one parent |
| `actor` | `Actor{type, id, verification}` | caller | the human (or system) who asked |
| `executor` | `Executor{type, id, agent, version, image_ref, binding}` | caller | which agent/service identity runs |
| `scope` | `Scope{tenant, repository, target_repository_sha, environment, service, slug}` | caller | where this execution may leave marks |
| `trigger` | `Trigger{type, ref}` | caller | what event carried the ask |
| `correlation` | `Correlation{temporal_workflow_id, temporal_run_id, argo_workflow_name, attempt}` | caller | the control-plane run ids the ledger and #195 traces already have |
| `assertions` | `Assertions{asserted_by, asserted_fields, declared_fields}` | caller | which fields the control plane vouches for vs which a workload declared |

Closed vocabularies:

- `actor.type`: `github_user | operator | cron | temporal-schedule | system`
- `actor.verification`: `control-plane-verified | signal-asserted | unverified`
- `trigger.type`: `github_issue | github_issue_comment | pull_request | incident | schedule | manual`
- `workflow_type`: `investigate | approve | implement | review-fix | incident | reconcile`
- `environment` (`scope.environment`): `production | shadow` (`dev_loop.py:80`, `resolver.py:110`)
- `executor.type`: reuses ADR 010's `Executor` vocabulary **verbatim** —
  `shepherd | pr-steward | devloop-workflow | reconciler | implementer` —
  **extended** with the SDK-backed agent names `docs/agent-inventory.yaml`
  lists that ADR 010's narrower *lifecycle-executor* vocabulary has no
  reason to know about (an `issue-poller` run never claims a PR):
  `issue-investigator | issue-poller | service-agent | incident-responder |
  mentor`. This is a **superset, not a divergence** — every ADR 010 value is
  still valid here (`tests/test_execution_identity.py::
  test_adr_010_executor_vocabulary_is_a_subset_of_this_schemas` pins it) —
  because ADR 010's `Executor` names things that can claim ownership of an
  entity, a narrower concept than "every agent execution this platform
  runs". A future ADR 010 revision that widens its own vocabulary does not
  need this ADR reopened; the reverse (an agent this ADR names claiming
  ownership) would.

No field here is authorization-shaped: `from_dict` rejects any key it does
not declare, and no field name may contain `allow`, `deny`, `permit`,
`grant`, `authorized` or `role` (sec. 5).

### 2. Identity and immutability

`content_hash = "sha256:" + sha256(canonical JSON of every field except
content_hash, context_id, issued_at)`, using `json.dumps(payload,
sort_keys=True, separators=(",", ":"))` — the same convention ADR 009 fixes
for `ContextSnapshot` and `orchestrator/resolver.py:301`'s `_hash_bytes`
convention. `context_id = "ex-" + content_hash[7:23]` — derived from content,
never random. `seal()` is the only constructor that fills `content_hash` and
`context_id`; there is no mutator. `issued_at` is excluded from the hash, so
sealing the same inputs twice at different times yields the same identity.

### 3. Step chaining

A root context has `parent_context_id: null` and `step_sequence: 0`. A child
context carries `parent_context_id` referencing its parent's `context_id`,
the same `trace_id` as its parent, and a strictly greater `step_sequence`.
`ExecutionContext.validate(parent=...)` enforces all three
(`tests/test_execution_identity.py`'s T5 section). This maps onto DevLoop's
investigate/approve/implement/review-fix steps, mirroring the root/child
pattern ADR 009 §3 already defines for `ContextSnapshot`.

### 4. Correlation with `ContextSnapshot` (ADR 009)

`ExecutionContext.to_execution_correlation(...)` projects the identity-owned
subset of `ContextSnapshot.ExecutionCorrelation`'s fields (`agent`,
`environment`, `temporal_workflow_id`, `temporal_run_id`,
`argo_workflow_name`, `target_repository_sha`) from `self`. The plan-owned
subset (`definition_version`, `definition_content_hash`, `profile_version`,
`profile_content_hash`, `release_revision`) is **not** carried on
`ExecutionContext` at all — those come from `orchestrator/resolver.py`'s
`ExecutionPlan`, a document resolved before submission and pinned by
publish-time hashes, a different lifecycle than an execution's runtime
identity — so a caller holding both a context and a plan supplies them as
keyword arguments. `orchestrator/context_snapshot.py` is not modified: the
dependency is one-way, `ExecutionContext` projects into
`ExecutionCorrelation`, never the reverse, exactly as ADR 009 §4 already
requires of every producer of that block.

### 5. Trust model

**The copy a workload holds is evidence, not authority.** The authoritative
copy lives in mctl-api, written only by the control plane
(`orchestrator/temporal/activities/identity.py`'s `mint_execution_context`
activity POSTs the sealed document to
`/api/v1/agents/executions/context`, next to the existing execution ledger).
A call presenting `X-Mctl-Execution-Context` is attributed by mctl-api
looking up that id server-side; any identity claim in a request body or
prompt is ignored server-side. `content_hash` makes local tampering
detectable — a modified copy no longer reseals to its `context_id`
(`recompute_content_hash`, T8) — and `executor.binding` records the Argo
workflow name a context was issued to, so a stolen id from a sibling run is
rejectable once mctl-api can observe the caller's own workflow. That last
check is the honest weak point today: every caller shares one admin
`MCTL_TOKEN`, so the binding is tamper-evident rather than tamper-proof. Real
per-run authentication is out of scope here (see "Non-goals").

`assertions` makes the trust model machine-readable: `asserted_by` is
`"control-plane"` for a control-plane-minted context and `"local"` for the
degraded fallback `mint_local()` produces; `asserted_fields`/
`declared_fields` are dotted paths a consumer checks membership against
rather than trusting the document wholesale.

**Fail-open outside the cluster, fail-closed when asked to.**
`load_from_environment()` reads the sealed context the CWFT wrote to
`MCTL_EXECUTION_CONTEXT_FILE` (a **file**, never an inline env value, so the
document never lands in an Argo parameter dump or a `printenv` in agent
logs). `MCTL_REQUIRE_EXECUTION_CONTEXT` decides what a failure means: with
it unset, an absent file degrades to an explicitly `actor.verification=
"unverified"`, `assertions.asserted_by="local"` context (`mint_local()`) so
local development and tests keep working, and a present-but-broken file
(unreadable, truncated, tampered) raises `ExecutionIdentityError`, which
the drivers catch narrowly and degrade the same way; with it set, both the
absent-file and broken-file cases raise `ExecutionContextRequiredError`
instead — deliberately NOT an `ExecutionIdentityError` subclass, so it
passes through the drivers' degrade handlers and kills the run rather than
proceeding silently unverified. A driver must never widen its catch to
cover it: `except ExecutionIdentityError` degrades, and
`ExecutionContextRequiredError` fails closed, by construction.

**Logging never risks a payload.** `to_log_dict()` returns the full
`to_dict()` — unlike `ContextSnapshot.to_log_dict()`, which drops `sources`,
`ExecutionContext` has no payload field anywhere in its schema for a log
emitter to leak, by construction (sec. 1's closed key set).

### 6. Boundary rules — normative and testable

| Concern | Owner | What `ExecutionContext` may record | What it must never do |
|---|---|---|---|
| Authorization (#197) | Policy checkpoints + provider-side enforcement | Nothing | No allow/deny/permit/grant/authorized/role field exists anywhere in the schema |
| Context relevance (ADR 009) | `ContextSnapshot` | `ExecutionContext.to_execution_correlation()` feeds ADR 009's `execution` block | Import `orchestrator/context_snapshot.py` at module scope (kept a call-time import so the stdlib-only module has no same-repo dependency it does not need) |
| Traces (#195) | Trace/telemetry pipeline | `to_log_dict()` — the whole document, since nothing in it is a payload | Nothing withheld; there is nothing to withhold |
| Evidence (#199) | Evidence store | Nothing — `ExecutionContext` names no evidence field | N/A |

Two tests make this executable rather than aspirational
(`tests/test_execution_identity.py`): a recursive field-name assertion that
no `allow`/`deny`/`permit`/`grant`/`authorized`/`role` token appears anywhere
in the serialized schema, and a subprocess import-direction assertion (the
style of `tests/test_worker_isolation.py`) proving
`orchestrator/execution_identity.py` loads stdlib only.

### 7. Sensitive data

No payload field exists: `from_dict` rejects unknown keys, and the only two
free-text-shaped fields — `trigger.ref` and `actor.id` — are bounded,
structural pointers (a GitHub issue/comment URL, a GitHub username), never a
place to smuggle a retrieved payload, matching the same discipline ADR 009
§7 states for `ContextSnapshot.locator`/`selector`.

## Alternatives

1. **Extend `ContextSnapshot.ExecutionCorrelation` instead of adding a new
   kind.** Rejected for the same reason ADR 009 itself rejected merging
   `ExecutionPlan` into `ContextSnapshot`: `ExecutionCorrelation` is
   explicitly a *caller-supplied copy*, never an assertion (ADR 009 §4/§5).
   Folding an asserted identity into it would make one document
   simultaneously untrusted input and trusted assertion. The chosen
   direction keeps the dependency one-way (sec. 4).
2. **A signed token (JWS/HMAC) carried entirely by the workload.** Rejected:
   it needs key distribution and rotation to every consumer, the workload
   holds the full signed claim set (replay across steps becomes the new
   problem), and the token would sit in an Argo parameter — the one place
   this repo already knows leaks into workflow specs and logs. The handle
   (`context_id`) plus server-side lookup gives the same attribution with
   nothing forgeable in the blast radius.
3. **Plain env vars (`MCTL_AGENT`, `MCTL_ACTOR`, ...) with no document.**
   Rejected: the agent process receives the entire parent environment and
   can rewrite it before spawning anything, there is no version, no
   vocabulary and no hash, and every consumer would re-derive a slightly
   different notion of "environment" — the exact drift this ADR exists to
   stop.
4. **Temporal search attributes / memo as the carrier.** Rejected as the
   sole propagation mechanism: useless past the Argo boundary (the agent
   container has no Temporal client by design, ADR 008) and it cannot reach
   an MCP call at all.

## Non-goals

- Any allow/deny decision, policy engine, policy checkpoint or enforcement
  logic — that is #197. This ADR adds no field a policy engine could read as
  permission.
- Replacing the shared static `MCTL_TOKEN` with per-run workload
  credentials. The `executor.binding` check narrows what a stolen context id
  buys; real per-run authentication is its own change in mctl-api and
  mctl-gitops.
- Implementing the #195 trace pipeline, exporters, or any OpenTelemetry
  wiring. This ADR only mints and propagates the `trace_id` that pipeline
  will consume.
- The mctl-api server-side storage implementation and the CWFT parameter
  declarations in mctl-gitops (see "Cross-repo prerequisites" below).
- Changing prompts, budgets, tools, models, or any agent behaviour.
- Evidence (#199) and context provenance payloads.

## Platform impact

- **Migrations:** additive only. New module, new ADR, no existing field
  changes meaning.
- **Backward compatibility:** every consumer treats a missing context as
  `unverified`, never as an error, outside the cluster
  (`load_from_environment()`'s degrade path).
- **Resource impact:** negligible — a sealed context is a few hundred bytes
  of primitives, one sha256 per seal.
- **Security:** the workload's copy is strictly non-authoritative. Real
  server-side attribution requires the mctl-api storage endpoint named
  below; until it exists, minting still improves the audit trail (a
  `context_id`/`trace_id` on the execution ledger and `.status.yaml`) even
  though no server-side lookup can yet reject a stolen id.

## Cross-repo prerequisites

Two sibling changes are named here as required follow-ups, each with a
compatible fallback so the mctl-agents side of this ADR is useful on its
own:

| Prerequisite | Repository | Fallback while absent |
|---|---|---|
| `POST /api/v1/agents/executions/context` storage endpoint, and `execution_context_id`/`trace_id` columns on the executions row | mctl-api | `mint_execution_context` degrades to a returned `unverified` context on any non-2xx response; the DevLoop step is never blocked |
| `execution_context_id`/`trace_id` parameter declarations on the four `mctl-agents-*` CWFTs, and writing the fetched context JSON to the file `MCTL_EXECUTION_CONTEXT_FILE` names, inside the agent container | mctl-gitops | Undeclared CWFT parameters already pass through (`orchestrator/temporal/workflows/incidents.py:100-106`), so the new keys ride along even before they are declared; `load_from_environment()` degrades to a locally-minted `unverified` context when the file is absent |

Until both land, the mctl-agents side of this ADR degrades to
mint-and-record-only: the control plane mints and posts a context per step
(a strict improvement to the audit trail even if mctl-api cannot yet store
it), but the sealed document never reaches the agent container, so
`mcp__mctl__*` calls and `.status.yaml` carry a locally-minted, explicitly
`unverified` identity rather than the control-plane-asserted one.

## Implementation map

This proposal changes:

```
docs/adr/011-execution-identity-contract.md                  # this file
docs/adr/009-context-snapshot-contract.md                    # one cross-link line
docs/agent-inventory.yaml                                    # one cross-link line
orchestrator/execution_identity.py                            # new module
orchestrator/temporal/activities/identity.py                  # new mint activity
orchestrator/temporal/worker.py                                # registers the activity
orchestrator/options.py                                        # X-Mctl-Execution-Context / X-Mctl-Trace-Id headers, AUDIT line
orchestrator/run_issue_investigator.py                          # loads/logs context, execution: block in .status.yaml
orchestrator/run_implementer.py                                 # loads/logs context, execution: block on status transitions
orchestrator/run_shepherd.py                                    # loads/logs context
tests/test_execution_identity.py                                # new tests
tests/fixtures/identity/investigator-context.json               # new fixture
tests/test_temporal_activities.py                                # mint activity tests
```

**Deferred, tracked as follow-up work, not part of this change:** minting a
root-plus-per-step context from inside `DevLoopWorkflow` itself, propagating
`execution_context_id`/`trace_id` across the `_run_cwft` Argo boundary, and
extending `ExecutionRecord` with the two new ledger fields. That wiring
touches `DevLoopWorkflow`'s replay-sensitive command sequence
(`orchestrator/temporal/workflows/dev_loop.py`) and its ~3900-line test
suite, and belongs in its own change behind `workflow.patched
("execution-identity")`, verified against the existing recorded histories in
`tests/test_workflow_replay.py` before it merges — exactly the caution this
repo's own patch-memoization lesson (`tests/test_patch_memoization.py`,
referenced in `dev_loop.py`'s module docstring) asks for. Everything in this
ADR's contract (sec. 1-7) is normative for that follow-up: it may add mint
call sites, but must not reopen the schema, the hash derivation, the step-
chaining rule, or the trust model.

The contract is normative for every other follow-up too. A follow-up may add
producer/mint/persistence implementation detail, but must not reopen: the
field shape and vocabulary table (sec. 1); the `content_hash`/`context_id`
derivation (sec. 2); the root/child step-chaining rule (sec. 3); the
one-way projection into `ExecutionCorrelation` (sec. 4); the trust model and
fail-open/fail-closed rule (sec. 5); or the boundary table and the "identity
is never authorization" sentence (sec. 6).
