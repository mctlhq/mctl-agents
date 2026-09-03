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

## Rollback

Set `ISSUE_INVESTIGATOR_RESOLVER_MODE=legacy` (already the default) —
`orchestrator/resolver.py` is then never imported or called by
`orchestrator/run_issue_investigator.py`. A full code rollback removes
`orchestrator/resolver.py` and restores
`agents/_manifests/issue-investigator/agent.yaml` to
`agents.mctl.ai/v1alpha1`; every other agent's manifest is untouched by
either change.
