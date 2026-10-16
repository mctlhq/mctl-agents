# Design: issue-596-test-agent-platform-add-an-inert-authori

## Current state

- Six manifests live under `agents/_manifests/<name>/agent.yaml`. Five are `agents.mctl.ai/v1alpha1` `kind: Agent`; only `issue-investigator` is `v1alpha2` `AgentDefinition`, and `tests/test_manifest.py::test_only_issue_investigator_is_v1alpha2` pins that. A v1alpha2 manifest loads its execution shape from the mctl-gitops profile via `orchestrator/resolver.py:load_profile`, so it cannot load before its profile exists (`orchestrator/manifest.py:_parse_fields_v1alpha2`).
- `orchestrator/manifest.py:_parse_fields_v1alpha1` requires `metadata.name` (equal to the directory name), `metadata.owner`, non-empty `spec.prompt.sources`, and `spec.runtime.type: claude-agent-sdk`; it reads `runtime.entrypoint`, `runtime.optionsBuilder`, `modelPolicy.task`, `toolPolicy.allow`, `execution.budgetUsd`, optional `execution.timeoutSeconds`, and `execution.sandbox.{backend,clusterWorkflowTemplate}`.
- `orchestrator/validate_manifest.py:validate()` checks per manifest: entrypoint and options builder resolve to callables; prompt sources exist (`inline:` symbols must exist); `modelPolicy.task` is a key of `config/model-policy.yaml` (`service_agent`, `mentor_digest`, `review_findings_normalize`); the sandbox CWFT exists among mctl-gitops `argo-workflows/cluster-templates/cwft-*.yaml`; `backend == argo`; `_check_tool_policy_and_budget_match_options_py` calls the real builder (with `MCTL_TOKEN` forced on, budget env vars cleared) and requires `set(toolPolicy.allow) == set(options.allowed_tools)` and `budgetUsd == options.max_budget_usd`; `timeoutSeconds` must be absent unless the agent is in `_TIMEOUT_CONSTANT_BY_AGENT`; `legacyEnvOverride` must be absent unless in `_LEGACY_MODEL_ENV_VAR_BY_AGENT`. `_builder_call_args` calls every builder except the investigator's as `(dir, model)`.
- Directory-wide checks: `check_manifests_match_inventory` requires a 1:1 name set with `docs/agent-inventory.yaml` and field equality for entrypoint, optionsBuilder, modelPolicyTask, sandbox and promptSources. `check_catalog_profiles_match_builders` iterates mctl-gitops profiles and errors on any profile not in `_AGENT_BY_CATALOG_PROFILE` (lines 85-97; #470 already pre-mapped three profiles ahead of their existence). `check_binding_pins_match_definitions` iterates existing bindings only.
- `tests/test_agent_inventory.py::test_agents_match_the_options_builders` requires the set of `build_*_options` functions in `orchestrator/options.py` to equal the inventory's builders, so a new builder needs an inventory row and vice versa. Every `ClaudeAgentOptions(...)` in `options.py` must be the direct argument of `_scrubbed(...)` (enforced by `tests/test_usage_ledger.py`).
- `orchestrator/options.py:build_mentor_options` is the closest model: no hooks, read-mostly tools, a module-level budget constant.
- Release side: `tools/check_agent_bindings.py` evaluates every `agents/_manifests/*/agent.yaml` and reports an absent binding as `missing` unless listed in `tools/publish_agent_release.py:UNBOUND_AGENTS` (currently `frozenset()`, pinned by `tests/test_promotion_requires_binding.py`). The shepherd's definition-merge gate (`orchestrator/run_shepherd.py` around line 641-675) treats any diff under `agents/_manifests/` as needing a human merge decision.

## Proposed solution

Add one v1alpha1 agent with no caller. Files:

1. `orchestrator/run_authoring_canary.py` (new). Module docstring stating the canary is inert by design (#596, #470) and must not be imported or scheduled. Contents:
   - `PROMPT: str` — a fixed one-line instruction ("Reply with the single word OK. Do not use any tool.").
   - `async def run_authoring_canary(workdir: Path) -> None` — calls `ensure_auth_for_sdk()`, builds options with `build_authoring_canary_options(workdir, SERVICE_AGENT_MODEL)` and runs one `ClaudeSDKClient` query with `PROMPT`, draining the response like `orchestrator/run_mentor.py:run_mentor`. No `os.getenv` model override (so no `legacyEnvOverride`), no `main()`, no `if __name__ == "__main__"` block, so `python -m` does nothing either. It genuinely calls the SDK, which keeps it an "agent" under the inventory's definition and keeps the manifest true; it is just never called.
2. `orchestrator/options.py`:
   - `AUTHORING_CANARY_BUDGET_USD = 0.01` — a plain constant, deliberately not `os.getenv`-backed, so there is no env knob to raise it and nothing to add to `_ENV_VARS_AFFECTING_OPTIONS_DEFAULTS`.
   - `AUTHORING_CANARY_TOOLS = ("Read", "Glob", "Grep")`.
   - `def build_authoring_canary_options(agent_dir: Path, model: str) -> ClaudeAgentOptions` returning `_scrubbed(ClaudeAgentOptions(cwd=str(agent_dir), model=model, allowed_tools=list(AUTHORING_CANARY_TOOLS), setting_sources=[], max_budget_usd=AUTHORING_CANARY_BUDGET_USD))`. No `mcp_servers`, no `_mctl_tool_globs()`, no hooks, default permission mode (no `acceptEdits`). The `(dir, model)` signature matches `_builder_call_args`' default branch, so the validator needs no change there.
3. `agents/_manifests/authoring-canary/agent.yaml` (new), v1alpha1:
   ```yaml
   apiVersion: agents.mctl.ai/v1alpha1
   kind: Agent
   metadata:
     name: authoring-canary
     owner: mctl-agents
   spec:
     runtime:
       type: claude-agent-sdk
       entrypoint: orchestrator.run_authoring_canary:run_authoring_canary
       optionsBuilder: orchestrator.options:build_authoring_canary_options
     prompt:
       sources:
         - inline: orchestrator/run_authoring_canary.py:PROMPT
     triggers: []          # informational; inert by design (#596)
     modelPolicy:
       task: service_agent
     toolPolicy:
       allow: [Read, Glob, Grep]
     execution:
       budgetUsd: 0.01     # AUTHORING_CANARY_BUDGET_USD
       sandbox:
         backend: argo
         clusterWorkflowTemplate: mctl-agents-run   # nominal; the CWFT never invokes this agent
   ```
   With header comments explaining the inertness and pointing at `tests/test_authoring_canary.py`. No `timeoutSeconds`, no `legacyEnvOverride`, no `serviceSkills`.
4. `docs/agent-inventory.yaml`: a seventh `agents:` row with `name: authoring-canary`, `driver: orchestrator/run_authoring_canary.py`, the same entrypoint/optionsBuilder/promptSources/modelPolicyTask/sandbox as the manifest, `writes: nothing`, `riskLevel: low`, `triggeredBy: []` and a comment that it is a standing fixture for #470 with no caller. No `budgetEnv` (there is no env var).
5. `orchestrator/validate_manifest.py`: one entry `"authoring-canary-default": "authoring-canary"` in `_AGENT_BY_CATALOG_PROFILE`, with a comment referencing #596, mirroring the #470 pre-mapping comment. No other validator change.
6. `tests/test_authoring_canary.py` (new) — the inertness guard (see tasks T1-T6).

Why v1alpha1: a v1alpha2 manifest cannot load until `authoring-canary-default` exists in mctl-gitops (out of scope), and `test_only_issue_investigator_is_v1alpha2` forbids it. The binding in mctl-gitops will pin the sha256 of this v1alpha1 file exactly like the other five v1alpha1 agents' bindings do.

Why no change to `config/model-policy.yaml`: `service_agent` already exists and is already in mctl-gitops `policy.yaml` `knownModelPolicyTasks`, so neither repo needs a new task and the follow-up profile can declare `modelPolicyRef.task: service_agent`.

CI expectation: the binding check reports `authoring-canary: missing` until the mctl-gitops binding merges. That is the gate working; `UNBOUND_AGENTS` is not touched.

## Alternatives

- **v1alpha2 AgentDefinition with `executionProfileRef: authoring-canary-default`.** Matches the long-term contract, but cannot load (and so cannot validate) before the mctl-gitops profile exists, which the issue forbids touching, and it breaks `test_only_issue_investigator_is_v1alpha2`. Dropped.
- **An entrypoint that raises immediately (`raise RuntimeError("inert")`).** Even smaller, but then nothing calls the SDK and the inventory's definition of an agent ("it calls the Claude Agent SDK") would be false for this row, and the prompt source would describe nothing. Inertness is better enforced by the absence of any caller (tested) than by a body that cannot run. Dropped.
- **A dedicated `authoring_canary: cheap` model-policy task.** Cheaper if ever run, but requires a matching `knownModelPolicyTasks` change in mctl-gitops and widens this change for no behavior. Recorded as an open question.
- **Env-overridable budget (`os.getenv("AUTHORING_CANARY_BUDGET_USD", "0.01")`).** Consistent with the other builders, but adds a knob that could raise the budget of an agent meant to be inert and requires extending `_ENV_VARS_AFFECTING_OPTIONS_DEFAULTS`. Dropped.

## Platform impact

- Migrations: none. No data, no schema, no deploy change. The image gains one unused module.
- Backward compatibility: all six existing manifests, their bindings and their CWFTs are unchanged. `_AGENT_BY_CATALOG_PROFILE` grows by one entry that is never looked up until the profile exists.
- Resource impact: zero at runtime; nothing invokes the agent.
- Risks and mitigations:
  - Someone later wires the canary into a caller. Mitigation: `tests/test_authoring_canary.py` scans Python imports/references, `entrypoint.sh`, `Dockerfile`, `pyproject.toml`, `.github/workflows/`, the Temporal worker/workflows/activities, and (when present) mctl-gitops CWFT/CronWorkflow files.
  - Tool policy widens. Mitigation: the test pins the builder's `allowed_tools` to exactly `{Read, Glob, Grep}`, no `mcp_servers`, no hooks, and the manifest's `toolPolicy.allow` to the same set; the validator independently requires manifest == builder.
  - The red "binding hash / agent bindings" job is mistaken for a defect and "fixed" by adding the agent to `UNBOUND_AGENTS`. Mitigation: the PR body states the expected `missing`; `tests/test_promotion_requires_binding.py` already asserts `UNBOUND_AGENTS == frozenset()`. **[Operator correction 2026-10-06]** The same file's `test_every_manifest_is_classified_as_bound` requires every manifest directory to be in `_BOUND_AGENTS`; `authoring-canary` is added there in this change (tasks.md 5b), which is what that test's docstring prescribes for a new manifest.
  - Automation merges the PR. Mitigation: the diff touches `agents/_manifests/`, so the shepherd's definition-merge gate (#585) holds it for a human; the PR description says so.
  - The nominal `mctl-agents-run` sandbox reference complicates the follow-up profile's budget-vs-CWFT check in mctl-gitops. Mitigation: recorded as an open question for the mctl-gitops follow-up; nothing in this repo depends on it.
