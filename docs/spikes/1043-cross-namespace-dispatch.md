STATUS: API_BRIDGE_SELECTED

# Spike 1043: cross-namespace Nexus versus an authenticated mctl-api command bridge

Refs mctlhq/mctl-agents#1043. Documentation and contract spike only: no namespace,
Nexus endpoint, secret, permission or runtime configuration is created or changed.
The issue stays open for the owner's verdict. Labels used below: `verified` (source
read in this run), `assumed` (from documentation or prior design, not read at the
pinned tag), `not-observed` (no access in this run).

## Decision

`API_BRIDGE_SELECTED`. Decision rule (design.md): Nexus needs N1-N5 all `verified`.
N2 (per-endpoint caller-namespace restriction in OSS) and N5 (stock authorizer
evaluation of Nexus dispatch) are unverified, and the frontend has no client
authentication today, so Nexus cannot be selected. Bridge gates B1-B4 are
satisfiable on current versions because they reuse the existing mctl-api
lease/CAS pattern (`orchestrator/temporal/dispatcher.py`) and the `aar_` receipt
client (`orchestrator/action_approvals.py`). Nexus stays a later swap behind the
same command schema once a reviewed authorizer exists.

M1 runtime activation stays held until mctl-gitops#2456 enforcement is live,
whichever transport is chosen (denial tests D1-D6 depend on it).

## Environment and versions

| Item | Value | Source | Label |
|---|---|---|---|
| Temporal server | 1.31.0 (appVersion of chart 1.2.0) | `platform-gitops/bootstrap/templates/data/temporal.yaml` in mctl-gitops@de70b954e1a8 | verified (declared); live image not-observed |
| Temporal Helm chart | 1.2.0 | same file, `targetRevision: 1.2.0` | verified |
| admin-tools | `temporalio/admin-tools:1.31.0` | `infra-components/data/temporal/tenant-namespace-job.yaml` | verified |
| Python SDK | `temporalio==1.34.0` | `pyproject.toml`, `uv.lock` | verified |
| nexus-rpc | 1.4.0 | `uv.lock` | verified |
| mctl-agents image | 1.117.0 | gitops `services/admins/mctl-agents-worker/values.yaml` (design.md deployed-state check) | assumed (file not in this clone) |
| mctl-api image | 4.154.0 | `platform-gitops/bootstrap/templates/mctl-platform/mctl-api.yaml` | verified |
| mctl-api Go SDK | unknown | mctl-api `go.mod` not available | not-observed |
| Live server dynamic config, namespaces, Nexus endpoints | unknown | no read access to the `temporal` namespace | not-observed |
| jsonschema (test only) | 4.26.0 | `uv.lock`, now a direct dev dependency | verified |

## Nexus gates N1-N7

Upstream reads were against `temporalio/temporal` tag v1.31.0
(`common/dynamicconfig/constants.go`, first 100000 characters only).

| Gate | Verdict | Evidence |
|---|---|---|
| N1 Nexus available/enabled on OSS 1.31.0 | not-observed | Nexus dynamic config keys exist at v1.31.0 (`system.refreshNexusEndpointsLongPollTimeout`, `frontend.nexusRequestHeadersBlacklist`, `limit.endpointNameMaxLength`, ...). An enable switch and the frontend HTTP address were not located in the portion read. gitops declares no `dynamicConfig`, no HTTP address override. |
| N2 Caller namespace allowlist per endpoint in OSS | not-observed (assumed Cloud-only) | No allowlist key seen in the constants read. Endpoint caller allowlists are documented as a Temporal Cloud feature; OSS would need a custom authorizer. |
| N3 Who may create/update endpoints | not-observed | The `OperatorService` Nexus endpoint RPC role mapping at v1.31.0 was not read (the role map URL tried returned 404). Candidate: `temporal-system:admin` per ADR 0003 scope spelling (candidate only, unverified for 1.31.0). |
| N4 Provider worker needs no permission on control namespace | not-observed | Expected from `PollNexusTaskQueue` targeting the handler namespace; not read. |
| N5 Default authorizer evaluates Nexus dispatch with caller claims | not-observed | Not read. The frontend has no authentication at all today (`networkpolicy.yaml` header), so nothing could enforce it now. |
| N6 Cancellation, retries, errors | assumed | `nexus-rpc==1.4.0` ships with the pinned SDK; `OperationError`/`HandlerError` semantics not read at the pinned versions. Go SDK version not-observed. |
| N7 Network and callback | verified (gap) | `networkpolicy.yaml` admits only 7233 and 9090; the frontend HTTP port Nexus needs and `component.callbacks.allowedAddresses` are not declared. |

Result: N1-N5 are not all verified, so `NEXUS_VERIFIED` is excluded.

## Bridge gates B1-B4

| Gate | Satisfiable on current versions | Basis |
|---|---|---|
| B1 provider reaches only mctl-api | yes | provider gets only a workload token scoped to the two bridge routes; any Temporal identity is `mctl-assistants-provider-glovo:write` only (#2456 design) |
| B2 commands issued only by control through mctl-api | yes | mctl-api creates `execution_id` and signs/binds the command |
| B3 idempotent by `command_id`, mutation single-use by `aar_` | yes | `action_approvals.py` consume is atomic and single-use; `intent_hash()` mirrors mctl-api |
| B4 results only from the leased worker, current lease, before expiry | yes | same CAS/lease model as `dispatcher.py` |

## Local disposable verification plan (not run)

Not authorized on the issue, so every `not-observed` gate stays so. Reproduction:

1. `temporal server start-dev` at 1.31.x with `--dynamic-config-value system.enableNexus=true` (confirm the key name first, see N1).
2. `temporal operator namespace create` for `ctl`, `prov`, `other`.
3. `temporal operator nexus endpoint create --name prov-ep --target-namespace prov --target-task-queue q`.
4. Run a handler worker in `prov`; call the endpoint from a workflow in `ctl` (expected: works) and from `other` (N2: record whether it is refused without a custom authorizer).
5. Attempt a `ctl` start/signal from the `prov` worker identity with the JWT authorizer enabled (N4/N5).
6. Record server logs, SDK versions and results in a revision of this memo.

Run status: not run.

## Contract schemas

`docs/spikes/1043/command.schema.json` and `result.schema.json` (JSON Schema
2020-12, `v1alpha1`, `additionalProperties: false`, every string bounded or
pattern-limited, opaque ref patterns, `aar_` required for `place_order_fake` and
`cancel_order_fake`, `ttl_seconds` at most 900). They are proposals consumed by no
service. Fixtures are in `docs/spikes/1043/fixtures/`. If Nexus is chosen later, the
same schemas define the operation input/output.

Route names (proposals for the later mctl-api issue, not existing routes):
`POST /api/v1/bridge/commands/claim`, `POST /api/v1/bridge/commands/{command_id}/result`.
Existing routes reused: `POST /api/v1/action-approvals/{id}/consume`.

## Call diagram

```
user/MCP facade --(OAuth, no Temporal creds)--> mctl-api
mctl-api --start/signal (control identity)--> [mctl-assistants] AssistantOrderWorkflow
AssistantOrderWorkflow --activity: create_bridge_command(execution_id, refs, aar_)--> mctl-api outbox (pending)
provider worker --POST claim (provider identity, lease)--> mctl-api  => command + lease_token
provider worker --POST consume aar_ (intent_hash)--> mctl-api  => granted | refused
provider worker --fake mutation (idempotency key = command_id)--> synthetic Glovo fake
provider worker --POST result (lease_token)--> mctl-api  => accepted | stale | duplicate
mctl-api --signal result (control identity)--> AssistantOrderWorkflow
```

The `aar_` receipt is consumed once, at the provider fake mutation, through
mctl-api's consume route; a second consume yields zero additional provider writes.

## Failure, retry and cancellation matrix

| Case | Behaviour | Provider writes |
|---|---|---|
| Crash after claim, before consume | lease lapses; reclaim under new lease token, same `command_id` | 0 then 1 |
| Crash after consume, before mutation | reclaim; consume answers already-consumed for same `command_id` + `intent_hash`; mutation keyed by `command_id` | 1 |
| Crash after mutation, before result | reclaim; fake keyed by `command_id` reports existing effect; `provider_effect=applied` | 1 |
| Duplicate result post | `duplicate`, no signal | 0 extra |
| Stale lease token | refused `stale_lease` | 0 extra |
| Command expired | claim refuses; control gets `expired` | 0 |
| Stale or rejected approval | consume refused; `approval_refused` | 0 |
| Cancel before claim | outbox row cancelled, never claimable | 0 |
| Cancel after claim | `cancel_requested=true` visible on heartbeat; before consume -> `cancelled`; after mutation -> compensating `cancel_order_fake` | 1 + compensation |
| mctl-api unavailable | provider backs off (exponential, cap 60 s, jitter); control create retries with same `command_id` | 0 extra |
| Control retry of create activity | same `command_id` (workflow id + step) returns existing row | 0 extra |

## Denial-test plan (executed in the later implementation issue)

| Id | Identity and call | Expected |
|---|---|---|
| D1 | provider identity, gRPC `StartWorkflowExecution` on `mctl-assistants` and `mctl-agents` | PermissionDenied |
| D2 | `SignalWorkflowExecution`, same namespaces | PermissionDenied |
| D3 | `QueryWorkflow`, same namespaces | PermissionDenied |
| D4 | `ListWorkflowExecutions`, same namespaces | PermissionDenied |
| D5 | `TerminateWorkflowExecution`, same namespaces | PermissionDenied |
| D6 | `DescribeWorkflowExecution`, same namespaces | PermissionDenied |
| D7 | provider token on any mctl-api route other than claim/result | 403 |
| D8 | result whose `tenant_ref`/`execution_id` differs from the leased command | refused, no signal |
| D9 | forged command (not from mctl-api, bad or replayed nonce) | provider refuses before consume |
| D10 | duplicate result callback | `duplicate`; fake write counter unchanged |
| D11 | stale approval (`aar_` expired or consumed for another intent) | consume refused; zero provider writes |
| D12 | MCP facade deployment has no Temporal address, token or CA mount | static gitops check passes |

D1-D6 require #2456 enforcement. Operator-permission test data for the Nexus path
must use the stock scope spelling `temporal-system:admin` (candidate permission,
unverified in server 1.31.0); a claim in any other spelling is an invalid scope and
must be rejected (covered by the offline test).

## Ownership map

| Party | Owns | Must not hold |
|---|---|---|
| mctl-api | only Temporal client for start/signal/query/list on `mctl-assistants`; user/tenant/principal/approval authority; bridge outbox, leases, nonce store, `aar_` consume | provider session secrets |
| mctl-agents | trusted control workflow code and worker on `mctl-assistants` | provider credentials |
| Provider worker | namespace `mctl-assistants-provider-glovo`; bridge claim/result | any role on `mctl-assistants` or `mctl-agents`; the mctl-api admin token (`MCTL_TOKEN`) |
| MCP facade | OAuth front door, delegates to mctl-api | any Temporal credential |

## Migration, rollback and cost

- Migration: greenfield. If a later spike verifies N2/N5, `create_bridge_command` can become a Nexus operation behind the same schema; in-flight commands drain on the old transport.
- Rollback: revert the PR (memo, `docs/spikes/1043/`, test, dev dependency line). Nothing else was touched.
- Cost: one outbox row and about 3-5 mctl-api requests per workflow; no model cost.

## Follow-up issue drafts (not filed)

Each needs its own approved DevLoop proposal.

1. mctl-api: bridge routes, outbox, leases, nonce store, provider workload token.
2. mctl-agents: control workflow on `mctl-assistants` with `create_bridge_command`.
3. mctl-agents: provider worker (claim, consume, fake mutation, result).
4. mctl-gitops: namespaces and identities, after #2456 enforcement.
5. Nexus re-evaluation spike with the local disposable plan above, if the owner authorizes it.
