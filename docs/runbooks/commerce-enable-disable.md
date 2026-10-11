# Runbook: enable and disable paid capabilities

Scope: simulated credits only, one tenant, the pilot provider
(`pilot-provider`, `lookup_status`). Real money, wallets, payouts and shared
tenants are out of scope (mctlhq/mctl-agents#631). ADR 020 sec. 14: mctl-api
owns all commerce state, and mctl-agents holds none.

Proof tenant: a dedicated `commerce-proof` tenant created by an operator with
the owner's approval. Never `admins`.

## Enable (in this order)

1. mctl-api: the commerce switch on, with `funds_mode: simulated` for the
   proof tenant and a EUR 1.00 (100 minor) simulated budget. Confirm with
   `GET /api/v1/commerce/tenants/{tenant}/budget` that `available_minor` is 100 and
   `funds_mode` is `simulated`.
2. Terms: a platform admin publishes the pilot terms (EUR 0.12,
   `per_successful_call`, provider `pilot-provider`). Confirm the current
   terms for `mctl://mcp-local/pilot-provider/lookup_status`.
3. mctl-agents, on `mctl-agents-worker-exec` only:
   `MCTL_COMMERCE_ENABLED=true`, `MCTL_COMMERCE_ADMITTED_PROVIDERS=pilot-provider`,
   `MCTL_COMMERCE_FUNDS_MODE=simulated`, `MCTL_COMMERCE_TENANT_ID=commerce-proof`.
   These are validated when a discovery-mode run builds its capability
   gateway, not at process start: a run with a funds mode other than
   `simulated`, an empty admission list or an unnamed tenant fails there,
   before any paid call. With `ISSUE_INVESTIGATOR_CAPABILITY_MODE=eager` they
   are never read, so confirm the worker runs in `discovery` mode.
4. Run the live proof (`uv run python tools/commerce_proof.py --backend live
   --out evidence/commerce-proof-<utc>.json`; `--out` is required), attach the
   bundle to #620, and confirm S14 shows zero non-terminal invocations. The
   scenarios share the proof tenant's budget, so the full run drives S3-S7 and
   skips S1 and S2, which each need an unspent EUR 1.00 budget: reset the
   budget and run `--only S1`, then reset again and run `--only S2`, each into
   its own bundle. S8-S13 are not driven live; `summary.not_driven` gives the
   reason for each, and S11 is offline-only by owner decision.

## Disable (the reverse)

1. mctl-api: turn the accept-only switch on (`COMMERCE_ACCEPT_DISABLED`,
   mctlhq/mctl-api#570). New quotes and acceptances are refused with HTTP 422
   `unsupported-execution`. Reserve, dispatch, finalize, release and the
   reconciler keep running. This is the authoritative stop and covers clients
   that ignore the env flag.
2. mctl-agents: unset `MCTL_COMMERCE_ENABLED`. No runtime is constructed and
   every paid descriptor is refused at invocation.
3. Keep the reconciler running. Never delete ledger rows.

## Check for outstanding liability

`GET /api/v1/commerce/tenants/{tenant}/invocations?phase=<phase>` for each
non-terminal phase (`accepted`, `reserved`, `dispatching`, `dispatched`,
`outcome_unknown`), or `CommerceClient.list_invocations`. Terminal phases are
`refused`, `released` and `finalized`. An invocation in `outcome_unknown` is
resolved by mctl-api's reconciler from the provider's outcome record; a
`finalized` one with `settlement_status: pending` is retried on settlement
only and is never dispatched again. The budget read's
`pending_liability_minor` must reach 0.

Alert on a `reconciliation_deadline` breach: it means an invocation has been
unresolved past its deadline and a human should look at its receipt.

## Reading the evidence

Telemetry is optional. The ledger and the receipts are the record, and they
are identical with telemetry off (S12). Metrics are listed in
`docs/observability/execution-traces.md`.
