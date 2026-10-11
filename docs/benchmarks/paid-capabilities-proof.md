# Paid capabilities proof (C5, mctlhq/mctl-agents#620)

One tenant, simulated EUR: the pilot capability
`mctl://mcp-local/pilot-provider/lookup_status` at EUR 0.12
`per_successful_call` against a EUR 1.00 budget (100 minor units). Contract:
ADR 020 (`docs/adr/020-capability-commerce-contract.md`), provider contract
versions `mctl-commerce-dispatch/v1`, `mctl-commerce-quote/v1` and
`mctl-commerce-terms/v1`.

## How to reproduce

```text
uv run python tools/commerce_proof.py --backend offline --out evidence/commerce-proof-offline.json
uv run python tools/commerce_proof.py --backend live --out evidence/commerce-proof-<utc>.json \
    --mctl-api-sha <sha> --image-tag mctl-agents=<tag> --image-tag mctl-api=<tag>
```

`offline` runs against the in-memory `FakeLedger` and `FakeApprovals`
(`tests/commerce_fakes.py`) and the real `ProviderIngress`. It runs in CI
(`tests/test_commerce_c5.py`). It proves the gateway and the provider. It does
not prove mctl-api's own atomicity. `live` needs the dedicated `commerce-proof`
tenant, `MCTL_TOKEN` for it, and for S7 a second tenant's token. The bundle
pins the mctl-agents and mctl-api commit SHAs, image tags, the three encoding
versions, the terms id and version, and the expected and observed result of
each scenario. It contains no arguments, results, tokens or payment proofs.

## Scenarios

| id | scenario | expected | offline | live |
|---|---|---|---|---|
| S1 | 12 concurrent distinct calls | at most 8 captured, the rest `budget-exhausted`, committed at most 96 | yes | `--only S1` on an unspent budget |
| S2 | 9th sequential call after 8 captures | `budget-exhausted`, committed 96 | yes | `--only S2` on an unspent budget |
| S3 | provider confirms failure | reservation released, available restored | yes | yes |
| S4 | same key, same arguments | one invocation, one capture, one execution | yes | yes |
| S5 | same key, changed arguments | `idempotency-conflict`, original unchanged | yes | yes |
| S6 | provider called without or with a forged authorization | refused, nothing executed | yes | yes |
| S7 | second tenant reads and invokes against the first | `not-found`, nothing changed | yes | needs a second token |
| S8 | permission revoked after accept and after reserve | refused / released, no dispatch | yes | skipped |
| S9 | approval pending, then denied | no funds held, refused with no reservation | yes | skipped |
| S10 | gateway killed after accept, reserve, the fence, the provider run | at most one execution and one capture | yes | skipped |
| S11 | capture settlement `unknown` after success | `settlement_status: pending`, retried on settlement only, never re-dispatched | yes | offline-only (owner decision 2026-10-11) |
| S12 | telemetry on and off | identical ledger totals and receipts | yes | skipped |
| S13 | commerce disabled with one `outcome_unknown` invocation | flag off: `provider-error` from the fence; switch on: `unsupported-execution`; the pending one reconciles | yes | skipped |
| S14 | liability sweep | zero non-terminal invocations, or each one named | yes | yes |

Live, every scenario shares the proof tenant's budget and ledger. Each sees
only the invocations it created and compares budgets as deltas, and each live
world gets its own execution id so two scenarios never share an idempotency
key. A full live run drives S3-S7 and S14; S1 and S2 each need an unspent EUR
1.00 budget and run separately with `--only`. A `skipped` scenario carries its
reason in the bundle (`summary.not_driven`) and is never read as passed.

Notes on what each backend proves:

- S5: the gateway derives its ledger key from the arguments' digest, so a
  changed argument set through `capability_invoke` is a different call. The
  conflict is mctl-api's and the proof presents the original key with a quote
  for other arguments.
- S7: the read and write routes answer `not-found` for a foreign and for a
  missing id, identically. An `invoke` by the other tenant is refused (it has
  no budget, so the gateway answers `budget-exhausted`) and changes nothing.
- S11: mctl-api has no fault-injection hook (owner decision 2026-10-11), so
  the settlement-only retry is proven offline only, and the bundle records it
  as `offline-only`, never as passed live.
- S13: while the accept-only switch is on, the expected reason code is
  `unsupported-execution` (no ADR 020 amendment). With the mctl-agents flag
  off, no terms are read and the call goes down the legacy path; the provider
  ingress fence refuses it with `provider-error`, which the scenario asserts
  exactly.
- S14: complement test (`phase` not terminal) over every row this run
  created, plus tenant-wide reads of each known non-terminal phase. A read
  that fills a page fails the scenario rather than passing on a partial list.
  Every invocation an earlier scenario reported must appear in the
  run-scoped read; a run that created none, or `--only S14` on a clean
  tenant, passes.

## Result

Offline backend at the commit that introduced this document: 13 scenarios
passed and S11 recorded `offline-only`. The live run is an operator step
(task 10 of the proposal) against the `commerce-proof` tenant. Its evidence
bundle is attached to #620 and its SHAs and image tags are in the bundle; this
document is updated with the bundle name once it exists. Until then, the live
column above is not evidence.
