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
- Slice 3 (#509): the issue-investigator's discovery-mode construction site
  in `orchestrator/run_issue_investigator.py` — `_capability_mode()`, the
  discovery branch of `_run_agent`, and the discovery-only prompt block in
  `_build_prompt`. ADR 017 sec. 8's option B (`capability_invoke` submits
  every capability to the `PolicyCheckpoint`, not only
  `mutating`/`consequential` ones — a dated owner decision, 2026-09-26).
- Slice 4 (this proposal, mctl-agents half — part A): the catalog field is
  now PARSED and enforced here — `orchestrator/resolver.py`'s
  `spec.capabilityDiscovery` parsing (`ExecutionProfile`/`ExecutionPlan`
  gain `capability_discovery_enabled`/`capability_providers`),
  `orchestrator/run_issue_investigator.py`'s profile-permission preflight,
  a third `orchestrator/validate_manifest.py` check for profiles that
  declare `capabilityDiscovery.enabled: true`, and the benchmark harness
  (`tools/capability_bench.py`). The mctl-gitops half (part B: the schema,
  and `issue-investigator-default` actually declaring the field) and the
  live measurement (part C) are separate, tracked below.

None of this runs in production by default. Every mode below defaults to
today's unchanged path.

## What activates it

**The catalog permits, the env var activates (slice 4).** Two conditions
must both hold, and the env var alone is no longer sufficient:

- The resolved profile's `spec.capabilityDiscovery.enabled` must be `true`.
  Absent the field entirely (every profile today, until part B lands) means
  `false` — a profile that predates this field permits nothing. The
  profile's `providers` list, in declaration order, is what
  `CapabilityGateway.build()` is given; there is no longer a module-level
  provider constant.
- `ISSUE_INVESTIGATOR_CAPABILITY_MODE=discovery` — the deployment switch,
  read fresh per run (like `ISSUE_INVESTIGATOR_RESOLVER_MODE` itself — see
  `docs/resolver-pilot-status.md`). `eager` (the default, whether the
  variable is unset or set explicitly) is today's path: the model connects
  the remote mctl MCP server directly and `orchestrator.capability_gateway`
  is never imported.

`ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative` is still required first:
discovery mode only exists on top of a resolved `ExecutionPlan`, and is
production-blocked exactly like the resolver pilot itself (see
`docs/resolver-pilot-status.md`'s "What is blocked on a real registry").

Rolling back means unsetting `ISSUE_INVESTIGATOR_CAPABILITY_MODE` only (see
"Rollback" below); unsetting the resolver mode alone while discovery is
still set is a hard failure, not a rollback. `discovery` requested while the
profile does not permit it is the same fail-closed posture, also a
`SystemExit`, never a silent fallback to eager.

`legacy` + `discovery` (declarative resolver mode off but discovery mode on)
is rejected with `SystemExit` at the start of `_run_agent`, before any
options are built — never half-applied, and checked before the `MCTL_TOKEN`
preflight so this message wins even when both conditions are absent. So is
`discovery` without `MCTL_TOKEN` (mctl MCP not configured in this
environment), `discovery` under a profile that does not permit it
(`capabilityDiscovery.enabled` is not `true`), and `discovery` under a
profile that does not grant `mcp__mctl__*`: the gateway would have nothing
it is allowed to serve.

A provider failure (unreachable, times out, or any other discovery error)
fails the run with the failure's reason code. It never falls back to eager
options: a silent mode change would invalidate whatever the pilot run was
measuring.

## What is blocked

Not part of this proposal — the mctl-gitops half and the live measurement:

- **Part B (mctl-gitops, human-authored PR, after this merges).**
  `spec.capabilityDiscovery` as an optional `ExecutionProfile` schema
  property, the `issue-investigator-default` profile actually declaring it
  (starting `enabled: false`, so nothing changes on merge), and the matching
  profile-version/release-binding bump. Until it lands, every profile
  resolves `capability_discovery_enabled=False` — discovery cannot run in
  production regardless of the env var.
- **Part C (operator, after A and B).** The measured comparison of initial
  tool-schema bytes and end-to-end token usage between `eager` and
  `discovery` mode, for one fixed issue and target SHA, with the sample size
  stated (requirements.md's benchmark acceptance criterion). The harness is
  implemented (`tools/capability_bench.py`); running it against a live,
  paid pair of runs is not something an implementer pass can do — see
  `docs/benchmarks/capability-discovery.md`.

## Rollback

Unset `ISSUE_INVESTIGATOR_CAPABILITY_MODE` (or set it to `eager`, the
default either way): `_run_agent` builds options exactly as it does today,
and `orchestrator.capability_gateway` is never imported. This is the only
rollback needed for the mctl-agents half. Permanently forbidding discovery
regardless of the env var is one reviewed mctl-gitops line
(`capabilityDiscovery.enabled: false`, part B).

Do **not** roll back by unsetting `ISSUE_INVESTIGATOR_RESOLVER_MODE` alone.
With `ISSUE_INVESTIGATOR_CAPABILITY_MODE=discovery` still set, that is the
`legacy + discovery` combination above, which fails every run with
`SystemExit`. To leave the resolver pilot as well, unset the capability mode
first (or both together).

Reverting slice 4's PR restores the slice-3 constant provider and drops the
profile-permission preflight, the third `validate_manifest.py` check, and the
benchmark harness; reverting slice 3's PR removes the mode switch, the
discovery construction site, and the discovery prompt block entirely. Slices
1 and 2 (the contract and runtime modules themselves) are unaffected either
way — nothing in production constructs a `CapabilityGateway` until the
switch is flipped.
