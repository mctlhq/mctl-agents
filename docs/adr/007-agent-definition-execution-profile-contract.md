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

### 1. `AgentDefinition` — concrete identity and lifecycle intent

`AgentDefinition` owns the stable identity of a concrete agent: `name`,
`owner`, `purpose`, `promptSources`, `runtimeContextInputs`, `triggers`,
and exactly one `executionProfileRef`. The reference is
`{name, compatibility}`, not an exact profile version. `compatibility` is a
schema/API compatibility constraint that the catalog validates when an
environment release is created. Changing the referenced profile name or
constraint bumps the definition version; publishing a new compatible
profile version does not.

`AgentManifest` becomes the Git/GitOps serialization of the desired
`AgentDefinition`: `agents/_manifests/<agent>/agent.yaml` remains the
authoring location and gains `spec.executionProfileRef` under
`agents.mctl.ai/v1alpha2`. The execution fields currently in
`spec.runtime.optionsBuilder`, `spec.modelPolicy`, `spec.toolPolicy`, and
`spec.execution` move into a referenced profile file. During migration,
`load_all()` must support both v1alpha1 and v1alpha2 explicitly; an unknown
version continues to fail loudly. `validate_manifest.py` keeps its existing
"checked claim, not a second implementation" rule by resolving the profile
and comparing the combined definition/profile claim with the real options
builder.

### 2. `ExecutionProfile` — reusable execution constraints

`ExecutionProfile` is independently published and may be reused by multiple
definitions. It owns:

- `modelPolicyRef`: the model-policy task and compatibility contract;
- `skills`: versioned platform-skill references;
- `tools`: the client-side allow-list of tool names;
- `policyRef` and `permissions`: required environment, resource, action,
  filesystem, network, and mutation scopes;
- `budgetUsd` and `timeoutSeconds`: both required and bounded;
- `runtime`: entrypoint/options builder plus sandbox backend and approved
  ClusterWorkflowTemplate;
- `approval`: actions that require a human signal before mutation;
- `evidence`: evidence required from the execution.

`tools` is not authorization. The profile is a reviewed least-privilege
claim and validation input; MCP servers, GitHub, Kubernetes, and other
providers remain authoritative for caller authorization. A profile that
omits a security-sensitive scope does not inherit broader access: v1alpha2
validation fails closed.

The migration creates one profile per existing agent first. N:1 sharing is
supported by the contract but is introduced only when two profiles are
proven compatible; no current budget, tool, or permission boundary is
silently broadened to manufacture sharing.

### 3. Immutable published versions

`AgentDefinition` and `ExecutionProfile` are published independently as
immutable registry versions (manifest hash + git SHA + owned input hashes).
A profile-only budget or tool-policy change publishes a new profile version
without changing the definition version. A definition version contains the
profile name and compatibility constraint, never the concrete profile
version selected for an environment. The exact compatible pair is chosen
only by the environment release binding below.

### 4. Atomic environment release binding

mctl-api owns one immutable-history `ReleaseBinding` per agent and
environment:

```yaml
environment: production
definition:
  name: issue-investigator
  version: 3
profile:
  name: investigator-default
  version: 5
revision: 12
```

Promotion validates that the definition's `executionProfileRef.name` and
`compatibility` accept the selected profile version, then replaces the
active tuple atomically. There is no half-promotion and no independent
rollback of only the definition or profile. Rollback restores the exact
previous compatible tuple and records a new binding revision, preserving
history.

`resolve_agent_release` returns the tuple. `ResolvedRelease` gains
`definition_version`, `profile_version`, and `release_revision` alongside
the concrete image reference. This preserves independent publication while
preventing a definition from executing with a profile version it did not
declare compatible.

### 5. One execution pinned to the resolved contract

At workflow start the resolver materializes an immutable `ExecutionPlan`
containing the exact definition version, profile version, release revision,
concrete model/model-policy version, skill and prompt hashes, effective tool
and permission policy, budget, timeout, runtime/sandbox references, target
repository SHA, approval requirements, and evidence requirements.

`ExecutionRecord` stores these identifiers with the Argo workflow name and
result. Temporal history and Argo state describe the live run; the execution
record is the durable statement of what ran. Missing, disabled, ambiguous,
or incompatible v1alpha2 references are rejected before Argo submission.

### Lifecycle states and ownership

The words `draft`, `published`, `active`, `deprecated`, and `disabled` span
three different authorities and must not be stored as one global mutable
field on a resource:

| State | Authority | Meaning |
|---|---|---|
| `draft` | Git/GitOps | Desired definition/profile exists but has no immutable registry version. |
| `published` | Registry version | Immutable version exists and is eligible for a compatible release binding. |
| `active` | Environment `ReleaseBinding` | This exact definition/profile version pair is selected for one environment. It is derived per environment, never a global resource state. |
| `deprecated` | Registry version | No new release binding may select this version; existing bindings remain resolvable until changed. |
| `disabled` | Registry version | The version is unresolvable. Any binding that still points to it fails closed and must be rolled back or promoted to another compatible tuple. |

`AgentDefinition` and `ExecutionProfile` use the same version lifecycle
independently. Their activation is always the atomic pair:

| From | Transition | To | Guard |
|---|---|---|---|
| Git `draft` | `publish` | registry `published` | Schema, references, options-builder comparison, and policy checks pass. |
| compatible published pair | `promote(env)` | environment `active` binding | Definition constraint accepts profile; both versions are published and enabled. |
| `published` or currently bound version | `deprecate` | `deprecated` | Existing bindings may resolve; no new binding may select it. |
| `deprecated` | `disable` | `disabled` | High-risk audited action; subsequent resolution fails closed. |
| active binding revision | `rollback(env)` | prior active tuple | Exact prior definition/profile pair is restored atomically and revalidated. |

A version may therefore be active in `production`, merely published in
`shadow`, and deprecated for future promotions without contradiction.

### Source-of-truth boundaries

| Layer | Owns | Must not answer |
|---|---|---|
| Git/GitOps | Reviewed draft definitions/profiles and desired catalog state. | What actually ran. |
| mctl-api registry | Immutable versions, compatibility metadata, lifecycle of versions, and atomic environment `ReleaseBinding` history. | Whether a workflow is currently running. |
| Runtime `ExecutionPlan` / `ExecutionRecord` | Exact resolved versions and effective model/policy/runtime inputs for one run. | Current desired state or later promotions. |
| Temporal/Argo | Durable orchestration and live sandbox execution until retention/TTL. | Long-term provenance after retention expires. |

The runtime snapshot is created once and never follows later promotions.
`ExecutionRecord` outlives Temporal/Argo retention and is the audit source
for the exact contract that produced a result.

### Versioning, compatibility, and fail-safe rules

- **Schema:** v1alpha1 remains supported during migration; v1alpha2 requires
  a resolvable profile, policy, bounded budget/timeout, and approved runtime.
  Unknown API versions fail loudly.
- **Definition version:** bumps for owner, purpose, prompt sources, triggers,
  or a change to the profile name/compatibility constraint.
- **Profile version:** bumps for model, skills, tools, permissions/policy,
  budget, timeout, runtime, approval, or evidence.
- **Runtime context:** target-repository inputs are pinned by target SHA and
  never misrepresented as publish-time hashes.
- **Promotion:** validates and atomically records one compatible definition/
  profile pair. Profile publication remains independent; activation does not.
- **Rollback:** restores the exact previous pair, not two independently
  selected historical versions.
- **Legacy fallback:** only the explicitly enabled v1alpha1 compatibility path
  may treat "no release exists" as permission to use the CWFT baked-in
  default. The fallback must be observable and removable.
- **v1alpha2 fail-closed:** a missing release, missing profile, disabled or
  deprecated-for-new-promotion version, compatibility mismatch, unknown
  model/skill/tool/policy reference, unbounded budget/timeout, or unapproved
  sandbox prevents Argo submission. No baked-in default is substituted.

Worked examples: editing only `owner` bumps the definition version; editing
only `budgetUsd` bumps the profile version; promoting the new budget creates
a new atomic release binding; rolling back restores the previous exact pair.

### Mapping the three named agents

| Agent | Profile execution shape | Required permissions/policy | Approval boundary |
|---|---|---|---|
| `issue-investigator` | `service_agent`, $3 budget, Argo `mctl-agents-investigate`, current prompt/tool claim | Target repository read-only; controlled write only to its GitOps proposal path and proposal issue comment; no production mutation. | No approval for investigation; proposal remains `proposed`. |
| `implementer` | `service_agent`, $3 budget, 900s, Argo `mctl-agents-implement` | Target repository branch/commit/PR write; no merge; GitOps status write scoped to its proposal. | Human acceptance before code-authoring run. |
| `shepherd` | `review_findings_normalize`, Read-only SDK tools, no mctl MCP, $5 budget, Argo `mctl-agents-shepherd` | Read review state; merge only repositories allowed by ownership policy; no arbitrary MCP/Kubernetes access. | Approval and verified head/check state before merge. |

The tool names above remain grounded in existing manifests, while the new
permission rows make explicit what the current `riskLevel` and `writes`
fields imply. Providers continue to enforce these scopes server-side.

### Migration path

- Existing v1alpha1 manifests, registry versions, releases, and workflows
  remain valid with zero immediate migration.
- For one agent at a time:
  1. Author and validate its v1alpha2 `ExecutionProfile`, including explicit
     policy/permissions and bounded execution fields.
  2. Publish the profile version.
  3. Author a v1alpha2 `AgentDefinition` with
     `executionProfileRef: {name, compatibility}`.
  4. Resolve the combined claim through `validate_manifest.py` and prove it
     equals the legacy options-builder behavior.
  5. Publish the definition version.
  6. Create one atomic environment `ReleaseBinding` for the compatible pair.
  7. Run legacy/declarative equivalence tests before removing the
     compatibility flag.
- Migration of one agent never changes another agent's active binding.
- Rollback selects the prior binding revision; it never synthesizes a pair
  by rolling back each half independently.

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

- **Migrations:** additive; no existing row is invalidated by this ADR.
- **Backward compatibility:** legacy fallback is isolated to an explicit
  v1alpha1 compatibility mode. v1alpha2 is fail-closed.
- **Resource impact:** one profile-version lookup plus one compatibility
  validation at release time; executions consume the already validated
  binding and still validate enabled state before submission.
- **Security:** tools and permissions are separate. Missing policy or scope
  cannot expand access, and provider-side authorization remains mandatory.
- **Primary risk:** schema and resolver could implement different pairing
  semantics. The atomic `ReleaseBinding`, lifecycle authority table, and
  fail-safe rules above are normative for both follow-ups.
- **Naming risk:** documentation and code must say "execution profile" or
  "model-policy profile" until the narrower existing key is renamed.
- **Scope control:** this ADR changes no runtime code or current behavior.

## Implementation map

This PR changes only:

```
docs/adr/007-agent-definition-execution-profile-contract.md
docs/agent-inventory.yaml  # header cross-link only
```

The contract is normative for both children of `mctlhq/.github#18`:

| Follow-up | Decisions fixed here |
|---|---|
| `mctl-gitops#950` GitOps catalog/schema | v1alpha2 split, required policy fields, version lifecycle, atomic `ReleaseBinding`, fail-closed validation, exact-pair rollback. |
| `mctl-agents#227` runtime resolver | immutable `ExecutionPlan`, legacy-only fallback, v1alpha2 pre-submit rejection, exact resolved tuple and execution identity. |

Circulation against both follow-ups is the final acceptance gate for this
`proposed` ADR. A follow-up may add schema or API implementation detail, but
must not reopen: who owns desired state versus releases versus live runs;
whether activation/rollback is atomic; whether v1alpha2 may silently
fallback; or whether tool names alone constitute authorization. Questions
in any of those areas must be resolved in this ADR before it becomes
`accepted`.