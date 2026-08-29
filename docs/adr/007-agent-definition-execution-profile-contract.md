# ADR 007 — `AgentDefinition` and `ExecutionProfile` contract

> **Status:** proposed (pending circulation against the GitOps catalog-schema
> and runtime-resolver follow-up issues — see Implementation map)
> **Date:** 2026-08-29
> **Supersedes:** nothing — this is a design document, no code changes

## Context

`mctl-agents` already has real infrastructure for agent lifecycle:
`AgentManifest` (`orchestrator/manifest.py`, `agents/_manifests/*/agent.yaml`,
formalising the classification in `docs/agent-inventory.yaml`) describes each
of the six SDK-backed agents (issue-investigator, implementer, shepherd,
incident-responder, service-agent, mentor; question-author exists as a
seventh manifest but is not yet part of the dev-loop).
`orchestrator/validate_manifest.py` checks every manifest claim against the
real `orchestrator/options.py` builder it describes — the "manifest is a
checked claim, not a second implementation" contract this ADR must not
break. mctl-api (mctl-api#126, consumed here through
`orchestrator/temporal/activities/registry.py` and `state.py`) already
provides immutable published agent versions, per-environment releases
(`resolve_agent_release`), promotion/rollback (`mctl_promote_agent` /
`mctl_rollback_agent`), and an execution audit trail (`record_execution` /
`ExecutionRecord`).

What is missing is the conceptual seam the issue asks for: today one YAML
document (`AgentManifest`) conflates a concrete agent's identity and
lifecycle (owner, purpose, prompt, trigger) with a reusable execution shape
(model, tools, budget, timeout, sandbox). There is no version field and no
lifecycle-state field anywhere in `agent.yaml` today — a manifest simply
exists or doesn't, and `load_all()` treats the six/seven files as the whole
population, a duplicate name being a hard load error rather than a version
conflict. There is no `ExecutionProfile` a second agent could share, no
documented lifecycle state machine, and no single document that draws the
boundary between reviewed Git/GitOps state, mctl-api's immutable registry
rows, and Temporal/Argo's runtime-resolved execution. Without that document,
the GitOps catalog-schema and runtime-resolver follow-ups named in the
parent epic (mctlhq/.github#18) would each have to invent this boundary
themselves, risking three incompatible answers to the same question.

**Naming collision already in the codebase**: `config/model-policy.yaml`
defines `profiles:` (`cheap`/`balanced`/`strong` model-escalation tiers,
consumed via `spec.modelPolicy.task` → `config/model_policy.py`). This is an
unrelated, narrower concept than the `ExecutionProfile` this ADR defines
(which also carries tools, budget, timeout, sandbox, approval, evidence).
Throughout this document and in future code/docs, "execution profile" and
"model-policy profile" must be qualified explicitly — this ADR does not
rename `config/model-policy.yaml`'s `profiles:` key.

Two existing ADRs already document adjacent decisions at this boundary:
ADR-005 (reconcile loop on Temporal) and ADR-006 (phase 6: merge → deploy →
monitor). Both are written in the same Context / Decision / Non-goals /
Implementation map shape this ADR follows, and both frame their work as one
phase of a larger, external "the plan" referenced by the `mctlhq/.github#18`
epic. `context/decisions/` holds repo-internal ADRs in the Nygard template
for orchestrator language/runtime/tooling choices; `docs/adr/` is where
cross-cutting dev-workflow-control-plane architecture already lives, which
is why this document is ADR 007 in `docs/adr/`, not `context/decisions/`.

## Decision

This is a documentation-only change. No code, manifest, or registry change
ships with it — `AgentManifest`, `orchestrator/manifest.py`,
`orchestrator/validate_manifest.py`, and every agent's runtime behaviour are
unchanged by this ADR.

### 1. `AgentDefinition` — the concrete, lifecycle-managed resource

Fields: `name` (today's manifest directory name / `metadata.name`), `owner`
(`metadata.owner`), `purpose` (free text, new), `promptSources` +
`runtimeContextInputs` (already exist, unchanged shape — lifted verbatim
from `docs/agent-inventory.yaml`'s existing split), `triggers` (today
implicit in `docs/agent-inventory.yaml`'s `triggeredBy`; becomes an explicit
field), `lifecycleState` (new, see state machine below), and exactly one
`executionProfileRef` (new — a `{name, version}` pointer, not an inline
embed, so an `ExecutionProfile` can be published and promoted
independently).

`AgentManifest` becomes the on-disk GitOps serialization of the *desired*
(`draft`) `AgentDefinition` state: `agents/_manifests/<agent>/agent.yaml`
keeps existing as-is today, and gains a `spec.executionProfileRef: {name,
version}` field under a new `apiVersion: agents.mctl.ai/v1alpha2`. Its
current `spec.runtime.optionsBuilder` / `spec.toolPolicy` / `spec.execution`
fields move to live in the referenced profile's own file instead of being
inlined. This is additive, not a rewrite: `orchestrator/manifest.py`'s
`SUPPORTED_API_VERSION` gate already fails loudly on an unrecognized
version, so v1alpha1 and v1alpha2 manifests can be told apart cleanly during
a migration window, and `orchestrator/validate_manifest.py`'s "call the real
builder, diff the claim" contract carries over unchanged — it resolves
`toolPolicy`/`execution` from the referenced profile file instead of the
definition file.

### 2. `ExecutionProfile` — the reusable, independently versioned resource

Fields: `model` (references a `model-policy.yaml` *task*, kept as today's
indirection — deliberately not renamed, see the naming-collision note in
Context), `skills` (new: a list of platform-skill references, formalising
what `docs/agent-inventory.yaml`'s `skills.sourceOfTruth` note already says
the registry must not re-implement — `ExecutionProfile` references skill
names, `mctl-gitops`'s `platform-skills/catalog/` stays the store), `tools`
(today's `toolPolicy.allow`), `budgetUsd`, `timeoutSeconds`, `runtime`
(today's `runtime.type`/`entrypoint`/`optionsBuilder`,
`sandbox.backend`/`clusterWorkflowTemplate`), `approval` (new: whether this
profile's executions require a human signal before a mutating step —
formalises the atomic-approve pattern `DevLoopWorkflow` already implements
for the implement step), `evidence` (new: what execution evidence this
profile's runs must produce — a forward reference to #199, not implemented
here).

Cardinality: one `ExecutionProfile` may be referenced by more than one
`AgentDefinition` (the "reusable" the issue asks for), but nothing in
`docs/agent-inventory.yaml` today shows two agents that should actually
share one — every agent has a distinct budget and mostly distinct tool set.
The migration therefore creates one profile per existing agent (1:1) and
leaves N:1 sharing as a capability the schema supports but no migration
forces.

### 3. Immutable published versions

Unchanged from what mctl-api#126 already does for agents
(`mctl_publish_agent_version`, `mctl_list_agent_versions`); this ADR extends
the same mechanism to profiles: an `ExecutionProfile` gets its own version
row (manifest hash + git SHA), published independently of the
`AgentDefinition` that references it, so a profile-only change (e.g. a
budget bump) publishes without bumping the definition's own version.

### 4. Environment release/promotion state

Unchanged mechanism (`mctl_promote_agent` / `mctl_rollback_agent` /
`resolve_agent_release`), generalized to resolve two references per
environment instead of one: `(definitionVersion, profileVersion)`.
`ResolvedRelease` (`activities/registry.py`) gains a
`profile_version`/`profile_image_ref` pair alongside today's
`version`/`image_ref`; `_resolve()` in `dev_loop.py` becomes a two-part
lookup that still returns `None` on either half missing, preserving today's
exact fail-safe (fall back to the CWFT's own baked-in default image) rather
than inventing a new failure mode.

### 5. One execution pinned to exact resolved versions

`ExecutionRecord` (`activities/state.py`) gains `definition_version` and
`profile_version` fields alongside its existing `version`/`image_ref`/
`target_repo`. This is the "execution identity" #196 will build on. An
execution missing either version reference must be rejected by
`record_execution`'s schema (mctl-api side, out of scope here) rather than
silently accepted with an empty string, the way `image_ref=""` is silently
tolerated today for the pre-registry compatibility case — that pre-registry
tolerance is a compatibility shim to preserve, not a pattern to extend to
the new fields.

### Lifecycle states

Both `AgentDefinition` and `ExecutionProfile` share the same state machine,
independently instantiated per resource:

```
draft --publish--> published --release(env)--> active
published --deprecate--> deprecated --disable--> disabled
active --deprecate--> deprecated (existing releases keep resolving; no NEW
                                    environment may release this version)
deprecated --disable--> disabled (resolve_agent_release must treat this
                                    version as unresolvable going forward)
```

| State | Meaning |
|---|---|
| `draft` | Exists in Git/GitOps only, never published. Mirrors today's `agent.yaml` before any `mctl_publish_agent_version` call — this state already exists implicitly for every manifest today. |
| `published` | Has an immutable registry version, not yet released to any environment. |
| `active` | Released to at least one environment (`resolve_agent_release` returns it for that environment). |
| `deprecated` | Still resolvable where already released, but not eligible for new promotions. Mirrors `mctl_disable_tenant_skill`'s adjacent skill-lifecycle pattern in the same MCP surface. |
| `disabled` | `resolve_agent_release` must return `None`, routing through the exact same fail-safe path `_resolve()` already has for "never released." |

| From | Transition | To | Guard |
|---|---|---|---|
| `draft` | `publish` | `published` | Manifest passes `validate_manifest.py` |
| `published` | `release(env)` | `active` | `mctl_promote_agent` targets this version |
| `published` | `deprecate` | `deprecated` | No environment has released this version yet |
| `active` | `deprecate` | `deprecated` | Existing releases keep resolving; no NEW environment may release this version |
| `deprecated` | `disable` | `disabled` | `resolve_agent_release` treats this version as unresolvable going forward |

### Source-of-truth boundaries

Four authorities, restated as an explicit table so the schema/resolver
follow-up issues do not re-derive it:

| Layer | Owns | Example in this repo |
|---|---|---|
| Git/GitOps (`mctl-agents` `agents/_manifests/`, `mctl-gitops`) | Desired state: draft definitions/profiles, reviewed and PR'd | `agents/_manifests/issue-investigator/agent.yaml` |
| mctl-api registry | Immutable published versions + environment releases | `mctl_publish_agent_version`, `mctl_promote_agent`, `resolve_agent_release` |
| Runtime-resolved execution snapshot | The exact (definition version, profile version, image ref) one Temporal workflow pinned at start | `ResolvedRelease` in `activities/registry.py` |
| Temporal/Argo execution state | The actual run: workflow history, Argo pod/workflow object, until TTL expiry | `DevLoopWorkflow` history, `submit_and_wait`'s `WorkflowResult` |

These are non-overlapping: Git/GitOps never answers "what actually ran" (no
runtime visibility); the registry never answers "is this specific run still
in flight" (Temporal owns that); the runtime snapshot is a value, not a live
source (recorded once in `ExecutionRecord`, never mutated); Temporal/Argo
state is authoritative only until its own TTL
(`ttlStrategy.secondsAfterCompletion` for Argo, retention limits for
Temporal) — which is exactly why `record_execution` exists, to outlive that
TTL.

### Versioning and compatibility

- **`apiVersion` bump path**: `agents.mctl.ai/v1alpha1` → `v1alpha2`,
  additive only. `orchestrator/manifest.py`'s `SUPPORTED_API_VERSION` check
  already fails loudly on an unrecognized version, so a mixed v1alpha1/
  v1alpha2 population during migration is detectable, not silently
  ambiguous. `load_all()` needs to accept both versions during the
  transition window — a resolver-issue implementation detail, not decided
  here.
- **`AgentDefinition` version bumps**: triggered by a change to `owner`,
  `purpose`, `promptSources`, `runtimeContextInputs`, `triggers`, or the
  `executionProfileRef` pointer itself (which profile/version this
  definition points at) — i.e. anything about *identity or which profile is
  used*, not the profile's own contents.
- **`ExecutionProfile` version bumps**: triggered by a change to `model`,
  `skills`, `tools`, `budgetUsd`, `timeoutSeconds`, `runtime`, `approval`, or
  `evidence` — i.e. anything about *how* an execution runs. Independent of
  the definition version: a budget bump publishes a new profile version
  without touching the definition.
- **Worked example** (the two questions a reviewer must be able to answer
  from this ADR alone): editing only a manifest's `owner` bumps
  `AgentDefinition`'s version and leaves `ExecutionProfile`'s version
  unchanged; editing only its `budgetUsd` bumps `ExecutionProfile`'s version
  and leaves `AgentDefinition`'s version unchanged.
- **Prompt/skill input hashing**: carried over unchanged from
  `docs/agent-inventory.yaml`'s existing `promptSources` vs
  `runtimeContextInputs` split — `promptSources` (inputs this repo owns,
  fixed at publish time) feed the `AgentDefinition` version hash exactly as
  they feed today's manifest version hash; `runtimeContextInputs` (inputs
  that resolve per-run against a target repo's own SHA) are never hashed,
  pinned per run by the target git SHA instead, unchanged from today.
- **Environment-release rollback semantics**: unchanged mechanism, extended
  symmetrically. `mctl_rollback_agent`'s existing "revert to the immediately
  prior promotion's `from_version`" behavior applies independently to each
  half of the pair — rolling back a definition release does not implicitly
  roll back its paired profile release, and vice versa, matching this ADR's
  "independently versioned" design.

### Mapping the three named agents

| Agent | Owner | `executionProfileRef` (profile carries) | Lifecycle state | Approval |
|---|---|---|---|---|
| `issue-investigator` | mctl-agents | model=`service_agent` task, tools=`Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Bash, mcp__mctl__*`, budget=$3.00, sandbox=`argo`/`mctl-agents-investigate` | `active` (already running today) | unset — `riskLevel: low`, read-only against the target repo (`docs/agent-inventory.yaml`) |
| `implementer` | mctl-agents | model=`service_agent` task, tools=`Read, Write, Edit, Glob, Grep, WebSearch, WebFetch, Bash, mcp__mctl__*`, budget=$3.00, timeout=900s, sandbox=`argo`/`mctl-agents-implement` | `active` (already running today) | required on its mutating step (commit/branch/PR) — `riskLevel: high`, "the only agent that authors code" (`docs/agent-inventory.yaml`) |
| `shepherd` | mctl-agents | model=`review_findings_normalize` task, tools=`Read` only, `mcp_servers={}` (no mctl MCP access at all), budget=$5.00, sandbox=`argo`/`mctl-agents-shepherd` | `active` (already running today) | required on its mutating step (merge) — `riskLevel: high`, "it merges to main" (`docs/agent-inventory.yaml`) |

Every cell above is sourced from the corresponding
`agents/_manifests/<agent>/agent.yaml` and `docs/agent-inventory.yaml` entry.
The `approval` field is unset for `issue-investigator` (nothing it writes is
a merge or a code change — gitops proposal files and an issue comment) and
required for `implementer` and `shepherd`'s mutating steps, which is how
this model preserves the `riskLevel`/`writes` distinction
`docs/agent-inventory.yaml` already draws instead of flattening all three
into one shape.

### Migration path

- **Zero required action**: existing `agent.yaml` files (v1alpha1), existing
  mctl-api registry versions, and existing environment releases remain valid
  with no change. No existing registry row is invalidated by merging this
  ADR — profiles are new rows alongside the existing agent table, not a
  rewrite of it.
- **One-time, per-agent opt-in migration** to v1alpha2 + profile split:
  1. Author a new `ExecutionProfile` file carrying the fields that move out
     of `agent.yaml`: `spec.runtime.optionsBuilder`, `spec.modelPolicy`,
     `spec.toolPolicy`, `spec.execution` (`budgetUsd`, `timeoutSeconds`,
     `sandbox.backend`, `sandbox.clusterWorkflowTemplate`).
  2. Publish that profile (`mctl_publish_agent_version`-equivalent for
     profiles), producing its first `published` version.
  3. Update `agent.yaml` to `apiVersion: agents.mctl.ai/v1alpha2`, remove the
     fields that moved, add `spec.executionProfileRef: {name, version}`
     pointing at the profile just published.
  4. Re-run `orchestrator/validate_manifest.py` to confirm the split
     resolves back to the same real `build_*_options()` claims as before the
     migration — this is the mechanical proof that the split changed no
     runtime behaviour.
  5. Publish and promote the updated `AgentDefinition` version through the
     existing `mctl_publish_agent_version` / `mctl_promote_agent` path.
- This sequence is additive at every step; an agent can stay on v1alpha1
  indefinitely without breaking anything the schema/resolver follow-ups
  build, since `SUPPORTED_API_VERSION`-gated dual support is part of the
  `apiVersion` bump path above.

## Alternatives

1. **Keep `AgentManifest` as a single flat resource; add lifecycle fields
   directly to it, no separate profile.** Rejected: it does not give the
   issue's required "reusable" profile — every field bump (a budget change)
   would still force a definition-version bump even when identity/prompt/
   trigger are unchanged, the exact coupling the issue asks to remove. It
   also does not resolve the issue's explicit ask for two distinct
   resources.
2. **Model `ExecutionProfile` as a pure registry-side concept with no
   GitOps file, configured only via MCP calls.** Rejected: every other
   agent-affecting input in this repo today is Git-reviewed (`agent.yaml` is
   a PR'd file); an MCP-only profile would be the one execution-affecting
   input with no code review trail, breaking the "reviewed Git/GitOps
   desired state" authority this ADR itself defines. It would also fragment
   validation — `validate_manifest.py` could no longer check profile claims
   against `options.py` the way it checks manifest claims today.
3. **Full N:1 sharing from day one — model `ExecutionProfile` as a small,
   curated set of named tiers that every `AgentDefinition` picks from,
   collapsing today's seven distinct budgets into three or four tiers.**
   Rejected for this proposal: it would require re-litigating each agent's
   actual budget/tool/timeout values, a behaviour change explicitly out of
   scope for an architecture-only issue. The chosen 1:1-by-default design
   supports this consolidation later as a pure follow-up (merge two profiles
   once they are proven identical) without blocking the contract on it now.

## Non-goals

- Implementing a resolver, a new catalog schema file, or any runtime code
  change.
- Rebuilding or migrating the existing mctl-api registry data model
  (mctl-api#126 stays as-is).
- Any catalog UI or new MCP operation.
- Activating a new agent or changing which agents exist today.
- Changing `issue-investigator`, `implementer`, or `shepherd` runtime
  behavior, prompts, tools, or budgets.
- The enabling work tracked separately (#149 isolated Argo execution, #195
  execution traces, #196 execution identity/context, #197 runtime policy
  checkpoints, #198 human approval, #199 execution evidence) — this ADR
  states how they plug into the model but does not implement them.
- Merging or starting implementation of the resolver/schema as part of this
  investigation.

## Platform impact

- **Migrations**: additive only (see Migration path above). Existing
  mctl-api registry rows (published agent versions, environment releases)
  are unaffected — profiles are new rows alongside them, not a rewrite.
- **Backward compatibility**: `resolve_agent_release`'s `None`-means-"fall
  back to CWFT default" contract is preserved and extended symmetrically to
  the profile half of the resolution — a target environment with no profile
  release yet behaves exactly like today's "no release yet" case, no new
  failure mode introduced.
- **Resource impact**: none from this issue directly (no code ships). The
  follow-up resolver issue will add one extra registry lookup per execution
  (profile resolve alongside definition resolve) — a follow-up cost, not one
  paid here.
- **Risks and mitigations**:
  - *Risk*: the ADR is written but the follow-up schema/resolver issues
    interpret it inconsistently. *Mitigation*: "follow-up schema and
    resolver issues can implement the contract without reopening ownership
    or lifecycle decisions" is a hard gate on this ADR's completeness — the
    state-machine and source-of-truth tables above are written to be
    copy-pasted into those issues directly.
  - *Risk*: "profile" naming collision with `config/model-policy.yaml`'s
    existing `profiles:` causes confusion in review or in future code.
    *Mitigation*: stated explicitly in Context above; code/docs must always
    qualify as "execution profile" vs "model-policy profile" until/unless a
    follow-up renames the latter.
  - *Risk*: scope creep — an implementer reads this ADR and starts building
    the resolver as part of "closing out" this issue. *Mitigation*: the
    Non-goals section above repeats the issue's non-goals verbatim, and this
    proposal's `tasks.md` contains no code tasks.

## Implementation map

No files change outside this document and the two cross-links noted below —
this ADR ships no code.

```
docs/adr/007-agent-definition-execution-profile-contract.md   # this file
docs/agent-inventory.yaml                                     # header cross-link only
```

This ADR unblocks two follow-up issues, both children of `mctlhq/.github#18`:

- **GitOps catalog-schema** (define the actual `agent.yaml` /
  `execution-profile.yaml` file shapes for `apiVersion: v1alpha2`): no
  longer needs to decide the `AgentDefinition`/`ExecutionProfile` field
  split, the lifecycle states/transitions, or which layer owns which
  question — all three are fixed by this ADR.
- **Runtime-resolver** (implement the two-part `resolve_agent_release`
  lookup and the `ResolvedRelease`/`ExecutionRecord` field additions): no
  longer needs to decide the fail-safe semantics for a missing profile
  reference, the version-bump triggers for definition vs. profile, or the
  execution-identity shape — all three are fixed by this ADR.

Before either follow-up issue starts implementation, this ADR must be
circulated against both (tasks.md task 6): confirming each author can answer
"can I implement this without reopening an ownership or lifecycle decision"
from this document alone. Any question raised during that circulation folds
back into this ADR's own sections, not into a new open question in either
follow-up issue.
