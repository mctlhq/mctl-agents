# Add an inert `authoring-canary` agent through the governed agent-authoring path

## Context

Issue mctlhq/mctl-agents#596 is the proof run for #470 (the governed agent-authoring path, parent roadmap mctlhq/.github#18). Every gate on that path is live: manifest validation (`orchestrator/validate_manifest.py`), the inventory tests (`tests/test_agent_inventory.py`), the shepherd's definition-merge gate on `agents/_manifests/` (`orchestrator/run_shepherd.py`, #585), the binding-hash / agent-bindings CI checks (`tools/check_agent_bindings.py`, `tools/check_binding_hash.py`) and the release promotion gate (`tools/publish_agent_release.py`, `UNBOUND_AGENTS` empty). What has not happened yet is one real change going through all of them. This proposal is that change.

The change adds a seventh agent, `authoring-canary`, that does nothing on purpose: a valid `v1alpha1` manifest, the smallest entrypoint and options builder that make the manifest true, read-only tools, the smallest budget the contract accepts, and no caller anywhere. Tests pin that inertness so that a later change which wires the entrypoint to a caller or widens its tool policy fails CI. The mctl-gitops execution profile `authoring-canary-default` and the shadow release binding are a separate, later change in mctl-gitops and are not touched here.

## User stories

- AS the platform owner I WANT one real agent addition to travel through validation, a reviewed PR, a human merge decision, a matching release binding and the release gate SO THAT #470 is proven on evidence, not on argument.
- AS a reviewer I WANT the new agent to be provably inert (no caller, read-only tools, minimal budget) SO THAT approving the proof run carries no operational risk.
- AS a future maintainer I WANT a test that fails the moment something starts invoking the canary or grants it a writing tool SO THAT the fixture cannot silently turn into a live agent.

## Acceptance criteria (EARS)

- WHEN `uv run python -m orchestrator.validate_manifest` runs over `agents/_manifests/` THE SYSTEM SHALL report `OK` for all seven manifests, including `agents/_manifests/authoring-canary/agent.yaml`, and the inventory, catalog, binding-pin, service-skills and admission checks SHALL stay green.
- WHEN `uv run pytest -q` runs THE SYSTEM SHALL pass, including `tests/test_agent_inventory.py`, `tests/test_manifest.py` and the new `tests/test_authoring_canary.py`.
- THE SYSTEM SHALL declare `authoring-canary` as `apiVersion: agents.mctl.ai/v1alpha1`, `kind: Agent`, `metadata.owner: mctl-agents`, with `runtime.entrypoint: orchestrator.run_authoring_canary:run_authoring_canary` and `runtime.optionsBuilder: orchestrator.options:build_authoring_canary_options`.
- THE SYSTEM SHALL make `build_authoring_canary_options` return `allowed_tools == ["Read", "Glob", "Grep"]`, no `mcp_servers`, no hooks, and `max_budget_usd == AUTHORING_CANARY_BUDGET_USD` (0.01), and the manifest's `toolPolicy.allow` and `execution.budgetUsd` SHALL mirror those values exactly.
- IF the canary's options builder or manifest grants any tool outside `{Read, Glob, Grep}` (for example `Write`, `Edit`, `Bash`, `WebFetch`, `WebSearch`, or any `mcp__*` tool) THEN THE SYSTEM SHALL fail `tests/test_authoring_canary.py`.
- IF any Python module under `orchestrator/`, `tools/` or `config/` other than `orchestrator/run_authoring_canary.py` itself imports or references `run_authoring_canary`, or any of `entrypoint.sh`, `Dockerfile`, `pyproject.toml` or `.github/workflows/*` references it, THEN THE SYSTEM SHALL fail `tests/test_authoring_canary.py`.
- IF the Temporal worker (`orchestrator/temporal/worker.py`), any Temporal workflow or activity, or (when the mctl-gitops checkout is present) any mctl-gitops `cwft-*.yaml` / `cronworkflow-*.yaml` mentions `authoring_canary` or `authoring-canary` THEN THE SYSTEM SHALL fail `tests/test_authoring_canary.py`.
- WHILE the mctl-gitops binding `releases/shadow/authoring-canary.yaml` does not exist THE SYSTEM SHALL report `authoring-canary` as `missing` in the binding check, and `UNBOUND_AGENTS` in `tools/publish_agent_release.py` SHALL remain `frozenset()`.
- WHEN the PR is opened THE SYSTEM SHALL route it through the shepherd's definition-merge gate (the diff touches `agents/_manifests/`), so the merge is held for a human decision and is not performed by automation.
- THE SYSTEM SHALL map `authoring-canary-default` to `authoring-canary` in `_AGENT_BY_CATALOG_PROFILE` ahead of the profile existing, so the follow-up mctl-gitops profile does not turn every mctl-agents PR red.

## Out of scope

- The mctl-gitops execution profile `agent-platform/execution-profiles/authoring-canary-default/profile.yaml` and the binding `agent-platform/releases/shadow/authoring-canary.yaml` (a follow-up issue in mctl-gitops, opened once this PR has a final `agent.yaml` to pin).
- Any change to `tools/publish_agent_release.py`, `tools/check_agent_bindings.py`, `tools/check_binding_hash.py`, `UNBOUND_AGENTS`, the shepherd definition-merge gate in `orchestrator/run_shepherd.py`, or any other agent's manifest.
- Any schedule, trigger, Temporal registration, CWFT/CronWorkflow wiring, MCP trigger, webhook or CLI entry point, now or "for later".
- Making the "binding hash" CI job green for `authoring-canary` before the mctl-gitops binding exists.
- A v1alpha2 `AgentDefinition` for the canary (it would need the not-yet-existing profile to load).

## Open questions

- Sandbox CWFT name. The validator (`_check_cluster_workflow_template`) and `tests/test_agent_inventory.py::test_cluster_workflow_template_exists` require `execution.sandbox.clusterWorkflowTemplate` to name an existing mctl-gitops CWFT, but the canary must not be wired into one. This proposal names `mctl-agents-run` as a nominal sandbox that does not invoke the canary, and a test asserts the CWFT never mentions it. The follow-up mctl-gitops profile will hit `validate-agent-platform.py`'s budget-vs-CWFT check (the profile's `budgetUsd` must match a `*_BUDGET_USD` env in that CWFT); how mctl-gitops satisfies that without wiring is a decision for that follow-up, not this change.
- Model-policy task. The issue says "if one is needed". This proposal reuses the existing `service_agent` task, so `config/model-policy.yaml` and mctl-gitops `policy.yaml` `knownModelPolicyTasks` need no change. A dedicated `authoring_canary: cheap` task is a reasonable alternative if the owner prefers it.
- Budget value. The manifest validator only requires equality with the builder; mctl-gitops' profile schema requires `budgetUsd > 0` (`exclusiveMinimum: 0`). `0.01` is chosen as the smallest practical positive value; the owner may prefer another.
