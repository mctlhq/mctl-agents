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
- Every input `execute()` reads is a **checked-in, explicitly
  non-promotable compatibility fixture** under `tests/fixtures/resolver/`:
  `profiles/investigator-default.yaml` (`metadata.promotable: false`) and
  `releases/issue-investigator.yaml` (`bindingSource:
  compatibility-fixture`, `promotable: false`). Neither is, or can be
  mistaken for, mctl-api registry or production-activation state.

## What is blocked on mctlhq/mctl-gitops#950

Production activation of the declarative resolver — flipping the default
away from `legacy`, migrating a second agent, or trusting a resolved plan
for an unattended production run — is blocked on #950's real GitOps
catalog schema, registry-backed compatibility validation, and atomic
`ReleaseBinding` API. Until then:

- The fixtures above are the only source `execute()` can resolve against;
  there is no live registry call anywhere in `orchestrator/resolver.py`.
- A fixture drifting from its pinned content hash (edited without
  re-pinning the release binding) fails resolution closed rather than
  silently promoting a different pair — see
  `orchestrator/resolver.py`'s module docstring.
- Migrating `implementer` or `shepherd` to v1alpha2, or removing the
  `ISSUE_INVESTIGATOR_RESOLVER_MODE` flag, is explicitly out of scope for
  this pilot (mctlhq/mctl-agents#227's requirements.md "Out of scope").

### Declarative mode is usable from a source checkout only

Worth stating plainly, because the flag looks like it would work anywhere:
the fixtures live under `tests/`, and the Dockerfile copies only
`orchestrator/`, `config/` and `agents/` into the image. `entrypoint.sh`
does not restore them either. So on the deployed container — the only place
`run_issue_investigator` normally runs — setting
`ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative` cannot succeed, by
construction rather than by accident.

`execute()` checks for the fixture root up front and says exactly that,
rather than reporting "missing execution profile", which reads like a
catalog typo and sends the reader looking for a file to add.

**#950 has since landed**, and the catalog now exists at
`platform-gitops/agent-platform/` — but it is itself marked
`bindingSource: compatibility-fixture` / `promotable: false`, so it is not
production activation either. It also diverges from the fixtures here
(profile named `issue-investigator-default` vs `investigator-default`,
`modelPolicyRef.task` `issue_investigator` vs `service_agent`, and a
different tool list). Converging the two — and settling which of them is
right about the investigator's real tool allow-list — is deliberately not
part of this pilot; it is tracked separately.

## Rollback

Set `ISSUE_INVESTIGATOR_RESOLVER_MODE=legacy` (already the default) —
`orchestrator/resolver.py` is then never imported or called by
`orchestrator/run_issue_investigator.py`. A full code rollback removes
`orchestrator/resolver.py`, `tests/fixtures/resolver/`, and restores
`agents/_manifests/issue-investigator/agent.yaml` to
`agents.mctl.ai/v1alpha1`; every other agent's manifest is untouched by
either change.
