# Declarative resolver — pilot status (mctlhq/mctl-agents#227)

`orchestrator/resolver.py`'s `execute(agent, task)` implements the runtime
seam ADR 007 (`docs/adr/007-agent-definition-execution-profile-contract.md`)
defines: an `AgentDefinition`, an independently published `ExecutionProfile`,
an atomic environment `ReleaseBinding`, and one immutable `ExecutionPlan` per
run. This document records what is, and is not, live today.

## What is live

- One agent, `issue-investigator`, has a canonical `agents.mctl.ai/v1alpha2`
  `AgentDefinition` at its existing manifest path
  (`agents/_manifests/issue-investigator/agent.yaml`).
- `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`
  (`orchestrator/run_issue_investigator.py`) resolves one `ExecutionPlan`
  per run and drives options from it. Default mode stays `legacy` — the
  unchanged pre-#227 path.
- Every input `execute()` reads comes from the **mctl-gitops agent-platform
  catalog** (`platform-gitops/agent-platform/`), which #277 made the source
  of truth: `execution-profiles/issue-investigator-default/profile.yaml` and
  `releases/shadow/issue-investigator.yaml`. Both are marked
  `bindingSource: compatibility-fixture` / `promotable: false`, so neither
  is, or can be mistaken for, mctl-api registry or production-activation
  state. The in-repo fixture tree that stood in for them until #277 step 4
  is deleted.

## What is blocked on a real registry

Production activation of the declarative resolver — flipping the default
away from `legacy`, migrating a second agent, or trusting a resolved plan
for an unattended production run — is blocked on a registry-backed
`ReleaseBinding` API in mctl-api. Until then:

- The catalog above is the only source `execute()` can resolve against;
  there is no live registry call anywhere in `orchestrator/resolver.py`,
  and a `bindingSource: registry` intent is refused rather than resolved.
- Migrating `implementer` or `shepherd` to v1alpha2, or removing the
  `ISSUE_INVESTIGATOR_RESOLVER_MODE` flag, is explicitly out of scope for
  this pilot (mctlhq/mctl-agents#227's requirements.md "Out of scope").

### What the version identifiers guarantee — and what they stopped
guaranteeing

While the profile lived under `tests/fixtures/`, its version WAS the sha256
of the file: editing it without re-pinning the release binding could not
resolve, so drift was impossible by construction.

The catalog versions profiles with a declared `spec.version` instead. That
is a claim about the content rather than the content itself — a profile
edited without a version bump resolves exactly as before, and nothing in
this repository detects it. Closing that needs a mctl-gitops CI check
comparing a profile's diff against its version bump.

The **definition** half is a different story, and pinned properly: the
binding's `spec.sourceManifest.contentHash` is the sha256 of `agent.yaml`,
recomputed on every resolution. That gate existed under the fixture, was
dropped when the profile moved to the catalog, and was restored after both
reviewers on #291 caught it independently — `definition.version` in the
catalog is `"1"`, a registry number naming no bytes, so without the hash the
definition floated while the profile was pinned. One pinned half and one
floating half is not an atomic binding.

The asymmetry is therefore deliberate: **the definition cannot drift
unnoticed; the profile's content can.** The first gap was recoverable from
this side, the second is not. The cost of closing the first is that editing
`agent.yaml` now requires a mctl-gitops PR to re-pin — the atomic-binding
discipline, not an accident of it.

What `execute()` also checks are the two cross-repository claims neither
repository's own CI can reach, because each can only read its own files:

- the binding's `spec.definition.profileCompatibility` still matches the
  definition's own `executionProfileRef.compatibility`. The binding schema
  documents that mirror as necessary "because mctl-gitops CI cannot read the
  mctl-agents source file" — which makes an unchecked mirror two sources of
  truth wearing one name;
- the binding's `spec.profile.version` still matches the profile's declared
  `spec.version`, and that version satisfies the definition's range.

The `ExecutionPlan` additionally records the sha256 of every file it read,
as provenance rather than as a gate: a plan says what it was built from even
where nothing forced those bytes to match a version number.

### Where the catalog lives at runtime

`orchestrator/resolver.py` finds it via `MCTL_GITOPS_ROOT`, falling back to
a sibling `../mctl-gitops/platform-gitops` checkout — the same resolution
rule `orchestrator/validate_manifest.py` uses, deliberately, so there is one
way to find that repository rather than two that can disagree.

- **In the image**, the repo root is `/app`, so the sibling fallback would
  resolve to `/mctl-gitops` and find nothing. The Argo CWFT clones
  mctl-gitops into the shared workdir and sets
  `MCTL_GITOPS_ROOT=/workdir/mctl-gitops/platform-gitops`
  (mctlhq/mctl-gitops#1004). Nothing under `tests/` ships any more.
- **In CI**, `pr-validation.yml` checks mctl-gitops out and sets the same
  variable.

Its absence is consequently not a normal condition anywhere, and
`execute()` checks for the catalog up front and says so — otherwise the
failure surfaces as "missing execution profile", which reads like a catalog
typo and sends the reader looking for a file to add when nothing is mounted
at all.

One consequence worth stating plainly: because `orchestrator/manifest.py`'s
v1alpha2 branch resolves `executionProfileRef` through this module, **loading
the issue-investigator manifest now requires a mctl-gitops checkout.** It
used to be self-contained. That is the cost of having one catalog instead of
two descriptions of one.

## Status as of 1.39.0 (2026-09-03)

Shipped: the resolver reads the mctl-gitops catalog (#277 step 4, #291), the
definition half of every binding is pinned by
`spec.sourceManifest.contentHash` and that pin is verified for **all three**
bindings rather than only the one the resolver reads (#293, #294), and a
profile whose content changes without a `spec.version` bump now fails
mctl-gitops CI (mctl-gitops#1009).

Unchanged: `ISSUE_INVESTIGATOR_RESOLVER_MODE` still defaults to `legacy`, so
nothing in production resolves declaratively. Shipping the code is not
activation.

## Service skills (mctlhq/mctl-agents#305)

Before this proposal, both `build_implementer_agent_options` and
`build_issue_investigator_options` (`orchestrator/options.py`) already set
`cwd=<target clone>` with `setting_sources=["project"]`, so the Claude Code
CLI loaded whatever `CLAUDE.md` / `.claude/agents/*.md` / `.claude/skills/**`
the target repository happened to ship — from the **mutable working tree**,
with no pin, no hash, no envelope check, and no record of which bytes were
loaded. `orchestrator/service_skills.py` replaces that ambient channel with
an explicit `ServiceSkillSet` contract:

- A target repository declares `.mctl/skills/manifest.yaml` (binding named
  mctl agents to skill ids) and `.mctl/skills/<id>/SKILL.md` files.
  Deliberately NOT `.claude/skills/`: that root is already loaded ambiently
  from the mutable worktree, and sharing one root would make "pinned bundle"
  and "ambient bundle" indistinguishable in both code and audit.
- Every read goes through `git ls-tree` / `git show` / `git cat-file`
  against a **pinned SHA**, never `Path.read_*` against the worktree
  (`service_skills._tree_entries`/`_blob_text`/`_blob_size`).
- The pinned SHA is `git rev-parse HEAD` for a read-only agent, but the
  **merge-base with `origin/HEAD`** for an agent-authored branch
  (`implementer`/`shepherd` on `feat/agents-*`) — `service_skills.pin_sha`
  — so a skill edit a previous run of the SAME agent committed cannot
  become policy for the next run without a human merging it into the
  default branch first.
- Enablement and ceilings (`enabled`/`root`/`maxSkills`/`maxSkillBytes`/
  `maxTotalBytes`) are a `spec.serviceSkills` block on the PLATFORM side:
  `agents/_manifests/<agent>/agent.yaml` for a v1alpha1 agent, or
  `ExecutionProfile.spec.serviceSkills` for a v1alpha2 agent once
  mctl-gitops's `execution-profile.schema.json` gains the field (a separate
  mctl-gitops PR). Neither is declared yet as of this proposal —
  `agents/_manifests/implementer/agent.yaml` has no `spec.serviceSkills`
  block (tasks.md task 13 remains open) — so both `implementer` and
  `issue-investigator` resolve `enabled: false` and read nothing today, same
  as any agent that never declared the block. The agent-to-skill BINDING
  lives in the target repo;
  the PERMISSION to read it plus its limits lives on the platform side —
  `ExecutionProfile.spec.skills` (the pre-existing platform-skill mechanism)
  is unrelated and unchanged. The platform-wide ceilings the gitops task-16
  PR adds to `agent-platform/policy.yaml` are `limits.maxServiceSkills`,
  `limits.maxServiceSkillBytes` and `limits.maxServiceTotalBytes` — the
  third bounds the aggregate `maxTotalBytes` and is compared only against
  it (`check_service_skills_limits`), never against the per-skill ceiling.
- `resolve_bundle()` is a type boundary, not a runtime check: a
  `ServiceSkillBundle` exposes only skill text and identifiers, never
  `allowed_tools`/`mcp_servers`/`permission_mode`/`max_budget_usd`/
  `policyRef`/a mutation scope, so a service skill physically cannot widen
  what a run may do. A skill's front matter declaring a reserved authority
  key (`tools`, `permissions`, `budgetUsd`, ...) rejects the whole bundle;
  `requiresTools` can only ever narrow a run by failing it.
- `ExecutionPlan` (declarative path) and the implementer's ad hoc call
  (legacy v1alpha1 path, `run_implementer._resolve_implementer_service_skills`)
  both record skill id / path / `sha256:`-prefixed content hash / byte
  count, plus the manifest's own hash and the pinned SHA — but never the
  skill TEXT, so `ExecutionPlan.to_log_dict()` stays small. New hashes carry
  the `sha256:` prefix; the pre-existing `skill_hashes` field (bare hex,
  hashing a platform-skill NAME rather than any content) is left as-is —
  see "What the version identifiers guarantee" above for the same kind of
  inconsistency accepted deliberately rather than silently changing an
  already-asserted value.
- Kill switch: `MCTL_SERVICE_SKILLS=off` makes every agent resolve an empty
  bundle and run zero git subprocesses, read fresh per call exactly like
  `ISSUE_INVESTIGATOR_RESOLVER_MODE` above — no redeploy needed to roll
  back.
- CI-usable validator: `python -m orchestrator.service_skills --validate
  <repo-path> [--agent NAME]` resolves a target repository's
  `.mctl/skills/**` against its worktree HEAD and prints every rejection, so
  a target repository can gate its own service-skill PRs before merging.

## Rollback

Set `ISSUE_INVESTIGATOR_RESOLVER_MODE=legacy` (already the default) —
`orchestrator/resolver.py` is then never imported or called by
`orchestrator/run_issue_investigator.py`. A full code rollback removes
`orchestrator/resolver.py` and restores
`agents/_manifests/issue-investigator/agent.yaml` to
`agents.mctl.ai/v1alpha1`; every other agent's manifest is untouched by
either change.
