# Capability discovery/gateway pilot — status (mctlhq/mctl-agents#242)

`orchestrator/capability.py` (the contract) and `orchestrator/
capability_gateway.py` (the runtime) implement ADR 017
(`docs/adr/017-capability-discovery-and-gateway-contract.md`): a sealed
`CapabilitySet` derived strictly from a resolved `ExecutionPlan`, and a
gateway serving `capability_search`/`capability_describe`/`capability_invoke`
over it instead of connecting the remote mctl MCP server directly. This
document records what is live, what activates it, what is still blocked, and
how to roll it back — mirroring `docs/resolver-pilot-status.md`'s shape for
the `#227` resolver pilot this work builds on.

## What is live

- Slice 1 (#485): the contract module, `orchestrator/capability.py` — frozen
  dataclasses, `seal()`/`validate()`, the `PolicyCheckpoint` seam,
  `AbsentPolicyCheckpoint`. Stdlib-only, worker-importable
  (`tests/test_worker_isolation.py`).
- Slice 2 (#508): the runtime, `orchestrator/capability_gateway.py` —
  `resolve_eligible()`, `CapabilityGateway` (discovery, search, describe,
  invoke), `PolicyDecidePolicyCheckpoint` (the `#197` adapter wrapping
  `orchestrator/policy_checkpoint.py`), and the `gateway=` parameter on
  `orchestrator/options.py::build_issue_investigator_options_from_plan`.
  Imports `claude_agent_sdk`/`mcp`, so it is never imported at module scope
  by anything worker-reachable — only inside the function that actually runs
  an agent (`tests/test_worker_isolation.py`'s
  `test_capability_gateway_is_never_imported_by_the_worker`).
- Slice 3 (this proposal): the issue-investigator's discovery-mode
  construction site in `orchestrator/run_issue_investigator.py` —
  `_capability_mode()`, the discovery branch of `_run_agent`, and the
  discovery-only prompt block in `_build_prompt`. ADR 017 sec. 8's option B
  (`capability_invoke` submits every capability to the `PolicyCheckpoint`,
  not only `mutating`/`consequential` ones — a dated owner decision,
  2026-09-26).

None of this runs in production by default. Every mode below defaults to
today's unchanged path.

## What activates it

Two environment variables, both read fresh per run (like
`ISSUE_INVESTIGATOR_RESOLVER_MODE` itself — see
`docs/resolver-pilot-status.md`), so an operator can roll back by unsetting
either one without a code change or a redeploy:

- `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative` — required first. Discovery
  mode only exists on top of a resolved `ExecutionPlan`; it is production-
  blocked exactly like the resolver pilot itself (see
  `docs/resolver-pilot-status.md`'s "What is blocked on a real registry").
- `ISSUE_INVESTIGATOR_CAPABILITY_MODE=discovery` — the pilot switch itself.
  `eager` (the default, whether the variable is unset or set explicitly) is
  today's path: the model connects the remote mctl MCP server directly and
  `orchestrator.capability_gateway` is never imported. `discovery` builds one
  sealed `CapabilitySet` for the `mctl-api` provider and serves
  `capability_search`/`capability_describe`/`capability_invoke` over it
  instead — `_run_agent` prints `[capability] capability_mode=<mode>` in
  every case, so the active mode is always observable in the run's log.

`legacy` + `discovery` (declarative resolver mode off but discovery mode on)
is rejected with `SystemExit` at the start of `_run_agent`, before any
options are built — never half-applied.

A provider failure (unreachable, times out, or any other discovery error)
fails the run with the failure's reason code. It never falls back to eager
options: a silent mode change would invalidate whatever the pilot run was
measuring.

## What is blocked (slice 4)

Slice 4 is not part of this proposal and needs its own slug:

- **The catalog field.** `spec.capabilityDiscovery: {mode, gatewayAlias}` on
  the mctl-gitops `ExecutionProfile` — a schema change, a validator change,
  and a release-binding bump in **mctl-gitops**, not this repository. Until
  it lands, `_capability_mode()`'s environment variable is the only switch;
  no catalog profile declares a mode.
- **`orchestrator/validate_manifest.py` mode-awareness.** The two
  set-equality checks there
  (`_check_tool_policy_and_budget_match_options_py`,
  `check_catalog_profiles_match_builders`) both call the profile's
  `runtime.optionsBuilder`, which is the legacy
  `build_issue_investigator_options`, not the plan/gateway builders — so the
  gateway path cannot make either check red yet. This task is paired with
  the catalog field above (it only matters once a profile can declare
  `capabilityDiscovery.mode`).
- **The benchmark.** A measured comparison of initial tool-schema bytes and
  end-to-end token usage between `eager` and `discovery` mode, for one fixed
  issue and target SHA, in both modes, with the sample size stated
  (requirements.md's benchmark acceptance criterion). This needs live paid
  runs an implementer pass cannot produce; slice 4's job.
- **The provider list stays a slice-3 constant.** `_run_agent` declares
  exactly one provider, `MCTL_API_PROVIDER` (`type="mcp-remote",
  id="mctl-api", alias="mctl"`), as a module-level constant. Moving the
  provider list onto the profile
  (`spec.capabilityDiscovery.providers`) is part of the catalog-field work
  above.

## Rollback

Unset `ISSUE_INVESTIGATOR_CAPABILITY_MODE` (or set it to `eager`, the
default either way): `_run_agent` builds options exactly as it does today,
and `orchestrator.capability_gateway` is never imported. Because discovery
mode also requires `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`, unsetting
*that* variable (already the default — see
`docs/resolver-pilot-status.md`'s own rollback note) has the same effect.

Reverting this proposal's PR removes the mode switch, the discovery
construction site, the discovery prompt block, and ADR 017 sec. 8's option-B
amendment. Slices 1 and 2 (the contract and runtime modules themselves) are
unaffected either way — nothing in production constructs a
`CapabilityGateway` until this switch is flipped.
