# ADR 020 — Capability commerce terms and execution contract

> **Status:** proposed
> **Date:** 2026-10-11
> **Issue:** mctlhq/mctl-api#546 (`commerce-contract`, the first required item
> of mctlhq/.github#173, `agent-commerce`)
> **Extends:** ADR 017 (capability descriptor, provider reference, gateway
> invocation). **Reuses:** ADR 012 (attribution, versioned pricing,
> deterministic dedupe), the action-approval intent encoding
> (mctl-api#366) and canonical principals (mctl-api#373, #428).
> **Supersedes:** nothing. This is a contract document. It ships no code,
> schema migration, endpoint or flag, and enables no payment behaviour.

## Context

ADR 017 answers *what an agent may see and call*: a sealed `CapabilitySet`
of `CapabilityDescriptor`s, each named `mctl://<provider_type>/<provider_id>/<tool>`,
checked against a `PolicyCheckpoint` before `capability_invoke` dispatches,
and recorded as a hashed `InvocationRecord`. ADR 012 answers *what a model
run cost the platform*: a deterministic, replay-safe usage record with a
`pricing_version`, stored in mctl-api beside `agent_executions`.

Neither can express what Agent Commerce needs: **an immutable customer charge
bound to one payer and one governed invocation**. Verified on mctl-agents
`29100d5` and mctl-api `30748f0`:

- `CapabilityDescriptor` has no price, and its MCP `annotations` are advisory
  (ADR 017 sec. 8). Nothing may read a price from them.
- `InvocationRecord` is a trace line. It is not durable and carries no payer.
- `internal/usage/types.go` stores model cost as `*float64` in USD with a
  session/result/model dedupe grain. That is provider expense, not customer
  spend, and binary floats are not money.
- `internal/workitems/action_approvals.go` binds one `intent_hash` to a
  single-use approval. `execution_requests.go` has claim tokens for fencing.
  Both are reusable; neither identifies an individual tool call.
- `internal/auth/principal.go` still lets a request proceed without a
  principal id when resolution fails (phase 1, `docs/principals.md`).

This ADR fixes the contract that C2 (`invocation-ledger`, mctl-api#547), C3
(`budget-authorizer`, mctl-api#548), C4 (`governed-invocation`,
mctl-agents#619), C5 (`paid-capabilities-proof`, mctl-agents#620) and the
optional C6 (`payment-interop`, mctl-api#549) are built against. The design
rationale and protocol research live in
[`mctlhq/.github` `docs/plans/agent-commerce.md`](https://github.com/mctlhq/.github/blob/main/docs/plans/agent-commerce.md);
they are not repeated here.

## Decision

### 1. Five facts, never collapsed

One invocation produces five independent facts. Each has its own field and
its own owner. No field may be derived from another, and none may be inferred
from an HTTP status, an MCP `isError` flag or a trace span.

| Fact | Question it answers | Owner |
|---|---|---|
| execution outcome | did the provider do the work? | the provider's outcome record (sec. 9) |
| metered usage | how much billable quantity occurred? | the meter in the pinned terms (sec. 4) |
| charge | what does the payer owe for it? | mctl-api ledger (C2) |
| payment status | what did the payment authorizer agree to? | `PaymentAuthorizer` (sec. 10) |
| settlement | did value actually move? | `PaymentAuthorizer` (sec. 10) |

**Customer price is not model cost.** A charge recorded here is customer
spend. ADR 012's `calculated_cost` is platform expense. They are stored
separately, in different units, and must never be summed into one number.
ADR 012's ledger is not modified by this contract.

### 2. Money

```yaml
money:
  currency: EUR        # ISO 4217 alphabetic code, upper case
  amount_minor: 12     # integer in the currency's minor unit, >= 0
```

- `amount_minor` is a signed 64-bit integer, constrained to `>= 0` on every
  price, quote, reservation and capture. A refund is a separate record
  (sec. 10), never a negative amount on an existing one.
- The minor-unit exponent comes from a checked-in ISO 4217 table, never from
  the caller. EUR has exponent 2, so `12` is EUR 0.12.
- Binary floating point is forbidden anywhere a monetary value is stored,
  hashed, compared or transmitted between MCTL components. Display
  formatting is the only place a decimal string is produced.
- An adapter whose rail uses an asset with its own scale (for example a
  6-decimal stablecoin) converts at the adapter boundary and records the
  rail amount in an opaque adapter reference. The core never stores an asset
  amount as if it were a currency amount.
- **One accounting currency per tenant.** A quote, reservation or capture
  in any other currency is refused with `currency-mismatch`. There is no FX
  and no cross-currency aggregation.

`funds_mode` travels with every quote, reservation, receipt and balance:

| `funds_mode` | Meaning |
|---|---|
| `simulated` | credits with no monetary value; cannot be redeemed, transferred, topped up or settled to a rail |
| `live` | real money; **forbidden** until a separately reviewed scope enables it |

A component that receives `live` before that scope exists refuses with
`funds-mode-unavailable`. The milestone is simulated credits only. A
simulated balance and a live balance are different accounts and never mix.

### 3. Identity and payer binding

Every commerce record binds these server-resolved identities. None of them
is ever read from a request body.

| Field | Source | Rule |
|---|---|---|
| `tenant_id` | the authenticated caller's tenant authority | required |
| `principal_id` | `User.PrincipalID()`, `prn_<ulid>` (mctl-api#373) | required — the actor that requested the invocation |
| `subject_principal_id` | the principal the action is performed for | required; equals `principal_id` when no delegation applies |
| `delegation_grant_id` | the execution-bound grant (mctl-api#428) | required whenever `subject_principal_id != principal_id`, else empty |
| `payer_account_id` | the tenant's budget account in `funds_mode` (C3) | required; in the milestone always the tenant's own account |
| `execution_id` | the `we_…` / `ex-…` correlation id (ADR 012) | required when the invocation runs inside an execution, else empty |

**Commerce boundaries fail closed on identity.** Phase 1 allows a request
with an unresolved principal to continue (`docs/principals.md`). Every
commerce endpoint refuses that request with `identity-unresolved` instead.
It does not wait for phase 2.

A wallet, card, payment token or signed payment proof identifies a payment
instrument. It never identifies a principal, a tenant or a payer account.

### 4. Commerce terms

Terms are the versioned price a provider declares for one capability.

```yaml
commerce_terms:
  schema_version: 1
  terms_id: ctm_<ulid>          # stable across versions
  terms_version: 3              # positive integer, strictly increasing per terms_id
  capability_id: mctl://mcp-remote/pilot-provider/lookup_status
  input_schema_hash: sha256:…   # ADR 017 descriptor field; pins the capability version
  provider_id: pilot-provider   # ADR 017 ProviderRef.id
  price_mode: per_successful_call   # free | per_call | per_successful_call
  meter: invocation             # the only meter in the milestone
  price: {currency: EUR, amount_minor: 12}
  funds_mode: simulated
  quote_ttl_seconds: 300        # 30..900
  effective_from: 2026-10-11T12:00:00Z
  retired_at: null
  published_by: prn_<ulid>
```

- **Capability version.** ADR 017 has no separate version field. The pair
  `(capability_id, input_schema_hash)` is the capability version. A schema
  change is a different capability version, so it requires new terms.
- **Immutability.** A published `(terms_id, terms_version)` is never
  modified. A price change publishes `terms_version + 1`. Retiring sets
  `retired_at` once. Rows are never deleted while any quote or invocation
  references them, the same rule ADR 012 applies to `pricing_version`.
- **Price modes.** The milestone has three. Anything else is refused at
  publication.

  | `price_mode` | Billable event | Quantity |
  |---|---|---|
  | `free` | none | 0, but usage is still recorded |
  | `per_call` | the provider **accepted** the dispatch (sec. 9) | 1 |
  | `per_successful_call` | the provider **confirmed success** (sec. 9) | 1 |

  For `per_call`, an accepted dispatch is billable even if the provider
  then fails or the response is lost. The quote states this.
- **Authority.** Terms are published by a principal authorized for the
  provider, never by the payer. In the milestone that means platform admins
  only. Provider self-service publication is out of scope.
- **Discovery is not authority.** Terms live in mctl-api (C2). The
  descriptor carries a read-only summary (sec. 12). An MCP annotation,
  catalog entry, Backstage card or Cloudflare Portal listing never defines
  or overrides a price.

### 5. Quotes

A quote is an immutable, server-minted offer: these terms, for this payer,
for these exact arguments, until this time.

| Field | Meaning |
|---|---|
| `quote_id` | `cq_<ulid>`, minted by mctl-api |
| `terms_id`, `terms_version` | the exact terms version priced |
| `capability_id`, `input_schema_hash` | the exact capability version |
| `tenant_id`, `payer_account_id`, `principal_id`, `subject_principal_id`, `delegation_grant_id`, `execution_id` | sec. 3 |
| `args_digest` | `sha256:` over ADR 017's `canonical_json(arguments)`, the same value `InvocationRecord.arguments_hash` carries |
| `price_mode`, `currency`, `amount_minor`, `funds_mode` | copied from the terms; `amount_minor` is the maximum the invocation can capture |
| `expires_at` | UTC, second precision, `created_at + quote_ttl_seconds` |
| `quote_digest` | below |

`quote_digest` uses the length-prefixed encoding mctl-api already uses for
`intent_hash` (`internal/workitems/action_approvals.go`). It involves no
JSON escaping, so any client can reproduce it byte for byte:

```text
mctl-commerce-quote/v1\n
<field>:<byte length>:<value>\n     # once per field, in this exact order
```

The order is: `quote_id`, `terms_id`, `terms_version`, `capability_id`,
`input_schema_hash`, `tenant_id`, `payer_account_id`, `principal_id`,
`subject_principal_id`, `delegation_grant_id`, `execution_id`,
`args_digest`, `price_mode`, `currency`, `amount_minor`, `funds_mode`,
`expires_at`. Integers are base-10 ASCII with no sign or leading zeros. An
empty optional field is the empty string. The result is `sha256:<hex>`.

**Golden vector.** C2 (Go) and C4 (Python) must both reproduce it in a test:

```text
quote_id              cq_01JZ8Q3K7W5V4X2Y1Z0A9B8C7D
terms_id              ctm_01JZ8PZ0000000000000000000
terms_version         3
capability_id         mctl://mcp-remote/pilot-provider/lookup_status
input_schema_hash     sha256:abab…ab   (64 hex chars, "ab" x 32)
tenant_id             acme
payer_account_id      acct_tenant_acme
principal_id          prn_01JZ8Q0000000000000000000A
subject_principal_id  prn_01JZ8Q0000000000000000000B
delegation_grant_id   (empty)
execution_id          we_01JZ8Q0000000000000000000C
args_digest           sha256:73fd22bf1a31b2661710f8e3413afb2705c876302e6cbf8a112aed3317130c8c
                      (= canonical_json({"query": "status"}))
price_mode            per_successful_call
currency              EUR
amount_minor          12
funds_mode            simulated
expires_at            2026-10-11T12:05:00Z

quote_digest = sha256:1a7b37d7215a2d02d9160f0afe49310e803eb2515f3745f0afb77995f8069131
```

Quote rules:

- **A quote grants nothing.** Holding a valid quote does not authorize an
  invocation. Execution re-runs identity, authorization and policy (sec. 7).
- **No silent repricing.** An invocation accepted on a quote is charged at
  that quote's terms, never at the latest terms. If the quote expired, or
  its terms version was retired before acceptance, the request is refused
  with `quote-expired` / `quote-stale`. The client must request a new quote,
  and any approval bound to the old one no longer matches (sec. 8).
- **Mismatch.** An invocation whose arguments, capability version, payer
  or identities differ from its quote is refused with `quote-mismatch`.
  Nothing is reserved and nothing is dispatched.
- Expiry is checked only when the invocation is first accepted. A replay of
  an accepted invocation (sec. 6) returns the original result even after
  the quote has expired.

### 6. Idempotency

- **Scope:** `(tenant_id, principal_id, idempotency_key)`. The key is
  client-supplied, 1–128 bytes of `[A-Za-z0-9._:-]`.
- **Request digest:** the `quote_digest`. The quote already binds the
  arguments, identities and price.
- **Same key, same digest:** return the original invocation handle and its
  current state. Never re-reserve, re-dispatch or re-charge.
- **Same key, different digest:** refuse with `idempotency-conflict`
  (HTTP 409). Nothing about the original invocation changes.
- **Invocation id:** `inv_<ulid>`, generated by mctl-api. It is never
  client-supplied.
- **Derived keys.** Every external call uses a stable key derived from the
  invocation id, so a retry after any crash is recognisable downstream:

  | Purpose | Key |
  |---|---|
  | provider operation | `mctl-op/v1:<invocation_id>` |
  | payment authorize / capture / void | `mctl-pay/v1:<invocation_id>:<authorize\|capture\|void>` |
  | refund (future) | `mctl-pay/v1:<invocation_id>:refund:<refund_seq>` |

- Idempotency rows live as long as the invocation record and have no TTL.
  A key that is retired while its invocation can still be reconciled would
  re-open double charging.
- MCP JSON-RPC request ids are transport ids. They are not business
  idempotency keys and never stand in for the keys above.

### 7. Execution order

Each step names the component that owns it. A failure at any step leaves the
state that section 11 defines for it.

| # | Step | Owner |
|---|---|---|
| 1 | Authenticate the actor and resolve `principal_id`, subject, delegation and tenant; refuse if unresolved | mctl-api |
| 2 | Resolve the capability from the sealed set (ADR 017 sec. 3) and check current tenant authorization | mctl-agents gateway + mctl-api |
| 3 | `PolicyCheckpoint` (ADR 017 sec. 6, ADR 014) | mctl-agents |
| 4 | Mint the quote and run a preliminary, non-binding budget check | mctl-api |
| 5 | Exact-action approval if policy requires it (sec. 8). **No funds are held while waiting.** | mctl-api approvals (#366) |
| 6 | Accept the invocation: idempotency row, quote validation, `phase = accepted` | mctl-api |
| 7 | Re-validate identity, authorization, policy and the quote; consume the approval if any | mctl-api + mctl-agents |
| 8 | Atomically reserve budget (C3) and authorize payment (sec. 10) | mctl-api |
| 9 | Persist dispatch intent with a fencing token, then dispatch with a signed invocation authorization (sec. 9) | mctl-agents (dispatch), mctl-api (intent, token) |
| 10 | Record the provider outcome | mctl-api, from the provider's outcome record |
| 11 | Capture or release, exactly once | mctl-api |
| 12 | Audit every transition (sec. 13) | mctl-api |

Budget exhaustion at step 4 or 8 is a hard deny (`budget-exhausted`). An
approval cannot override an exhausted hard limit. Raising a budget is a
separate, separately authorized act; it is not permission to perform the
action.

For `simulated` funds, the reservation and the authorization in step 8
commit in one database transaction. For a future live rail this is
impossible, because a database transaction cannot commit a remote payment.
Step 8 then becomes a durable intent plus outbox plus reconciliation. No
live rail is exposed before that is proven (C6 and a separate scope).

Paid streaming or long-running execution (MCP progress streams, MCP tasks,
any partial-result protocol) is refused with `unsupported-execution` before
step 6. Nothing is reserved for it.

### 8. Approval binding

A paid invocation that policy routes to approval uses the existing
action-approval contract unchanged:

| Intent field | Value |
|---|---|
| `action_kind` | `capability_invoke` |
| `target` | `capability_id` |
| `args_digest` | the quote's `args_digest` |
| `artifact_hash` | the `quote_digest` (the field is documented as an optional payload hash) |
| `execution_id`, `policy_rule_id`, `policy_version`, `work_item_id` | as today |

This keeps the encoding at `mctl-action-intent/v1`. An approver approves a
specific price, for specific arguments, for a specific payer. A new quote,
whether from a price change, expiry or different arguments, produces a
different `intent_hash`, so the old approval cannot be consumed for it.

An approval is consumed once, by one invocation, at step 7. The invocation
records the consumed `approval_id`. On restart, the consumed approval
resumes **that** invocation's reconciliation and never authorizes another.
No second approval service is introduced.

### 9. Paid provider requirements

A provider is eligible for `per_call` or `per_successful_call` only if it
meets all of the following. Otherwise its capabilities may be `free` or
legacy only. Providers declare this as `commerce_support`, and C4's tests
prove it before the provider is admitted.

1. **Ingress enforcement.** The provider verifies, at its own protected
   entry point, an invocation authorization signed by mctl-api. The
   authorization binds `invocation_id`, the provider operation key,
   `capability_id`, `input_schema_hash`, `args_digest`, `tenant_id` and
   `dispatch_deadline`. A call without a valid authorization is refused.
   Direct endpoint calls, eager-mode calls and alternate adapters therefore
   cannot reach a paid capability. A client-side wrapper is not a payment
   boundary.
2. **Durable dedupe.** A second dispatch with the same provider operation
   key returns the first operation's outcome and does not execute again.
3. **Acceptance record.** Accepting a dispatch durably records the key
   before any side effect. "Accepted" in section 4 means exactly this
   record.
4. **Dispatch deadline.** The provider refuses a key whose
   `dispatch_deadline` has passed and has not already been accepted. This
   makes a later `not_found` safe to act on.
5. **Outcome lookup.** `outcome(key)` returns one of the following:

   | Result | Meaning | MCTL may act on it |
   |---|---|---|
   | `succeeded` | accepted and completed successfully | capture (both paid modes) |
   | `failed` | accepted and completed unsuccessfully | `per_call`: capture; `per_successful_call`: release |
   | `in_progress` | accepted, not finished | wait |
   | `not_found` after `dispatch_deadline` | never accepted, and can no longer be | release |
   | `not_found` before `dispatch_deadline` | not yet accepted | nothing; not proof of absence |
   | timeout, transport error, malformed answer | **unknown** | nothing; never treated as `not_found` |

6. **Outcome authority.** Success is the provider's `succeeded` record.
   An HTTP 200 carrying an MCP tool result with `isError: true` is a
   failure. A missing response is unknown. A late response that contradicts
   a recorded outcome is audited and changes no charge.

Exactly-once **accounting** is required of MCTL for every paid invocation.
Exactly-once **effects** are claimed only for a provider that meets
requirements 2–5 and has passed the fault tests. For an arbitrary MCP
server, MCTL reports uncertainty instead of promising exactly-once
execution.

### 10. `PaymentAuthorizer`

The core talks to every payment rail, simulated or future, through one
interface. Every operation is idempotent on its derived key (sec. 6). The
same key with different parameters is `conflict`, and nothing changes.

| Operation | Input | Results |
|---|---|---|
| `authorize` | key, `invocation_id`, `payer_account_id`, `tenant_id`, money, `funds_mode`, `quote_digest`, optional `payment_proof` | `authorized{authorization_id}` \| `denied{reason}` \| `unknown` |
| `capture` | key, `authorization_id`, money (`<=` authorized) | `captured{capture_id}` \| `failed{reason}` \| `unknown` |
| `void` | key, `authorization_id` | `voided` \| `failed{reason}` \| `unknown` |
| `status` | any of the keys above | the recorded state \| `not_found` \| `unavailable` |

- `unknown` and `unavailable` are never `denied`, `voided` or `not_found`.
  An unknown authorization keeps the reservation held, and an unknown
  capture keeps the charge `settlement_pending`. Both are resolved only by
  `status` with the same key, never by issuing a fresh operation.
- **Feature negotiation.** An adapter declares `adapter_id`,
  `adapter_version`, the `funds_mode`s it supports and a feature set drawn
  from: `auth_capture`, `void`, `status_lookup`, `partial_capture`,
  `refund`, `async_settlement`, `simulation`. `auth_capture`, `void` and
  `status_lookup` are required for any paid mode. An adapter missing one is
  ineligible, and is refused at configuration time rather than at the first
  payment.
- **Settlement.** `settlement_status` is `not_applicable`, `pending`,
  `settled` or `failed`. Simulated captures are recorded `settled` with
  `funds_mode: simulated`. They move no value and cannot be redeemed.
- **Refunds (future).** A refund is an append-only `RefundRecord`:
  `refund_id`, `capture_id`, money, reason, actor, created_at and its
  derived key. It never edits or negates the original capture. No refund
  flow is in the milestone; the shape is fixed now so the ledger does not
  need a breaking change for it.
- **No blockchain dependency.** Rail-specific identifiers (transaction
  hashes, network ids, facilitator receipts, processor ids) live in one
  bounded, opaque `external_ref` string per record. The core never parses
  it, keys on it or stores a secret in it. An invoice or enterprise
  chargeback authorizer implements the same four operations.
- **Payment proof is never authorization.** A signed proof (x402, MPP or
  any successor) is an input to `authorize` only. It is valid only when
  bound to the server-authenticated payer, tenant, invocation and
  `quote_digest`. It never grants access to a capability, never satisfies
  steps 1–7, and never replaces a policy decision or an approval.

### 11. Durable state and failure semantics

An invocation has one lifecycle `phase`. The five facts from section 1 are
separate fields beside it.

```text
accepted ──► reserved ──► dispatching ──► dispatched ──► finalized
    │            │              │               │
    └► refused   └► released    ├► released     └► outcome_unknown ──► finalized
                                └► outcome_unknown
```

| `phase` | Meaning | Terminal |
|---|---|---|
| `accepted` | idempotency row and quote stored; nothing held | no |
| `refused` | rejected before any reservation; no charge | yes |
| `reserved` | budget held and payment authorized | no |
| `dispatching` | dispatch intent persisted under a fencing token; the provider has not confirmed acceptance | no |
| `dispatched` | the provider confirmed acceptance | no |
| `outcome_unknown` | the outcome cannot currently be observed; conservative liability is held | no |
| `released` | the reservation was voided or released; no charge | yes |
| `finalized` | the outcome is known and the charge is captured or released | yes (settlement may still be `pending`) |

Fields beside `phase`: `execution_outcome` (`not_dispatched | succeeded |
failed | unknown`), `usage_quantity` (integer, absent until known),
`charge_status` (`none | reserved | captured | released`),
`payment_status` (`not_required | authorized | denied | captured | voided |
unknown`), `settlement_status` (sec. 10), `reason_code`.

Transitions are written transactionally with an append-only history row
(C2). A transition that does not match the current `phase` is rejected,
never applied blindly.

| Case | Resulting state |
|---|---|
| identity, authorization, policy, approval or budget rejection | `refused`, `reason_code` set, no dispatch, no charge, refusal audited |
| quote expired, stale or mismatched | `refused` (`quote-expired` / `quote-stale` / `quote-mismatch`) |
| payment authorization denied | `released` (`payment-denied`), reservation released, no dispatch |
| failure before the provider accepts dispatch | `released`, `execution_outcome: not_dispatched`, no charge |
| cancellation before dispatch | `released` |
| cancellation after dispatch | no change until the provider confirms the cancellation or an outcome |
| confirmed provider failure | `finalized`, `failed`; `per_call` captures once, `per_successful_call` releases |
| confirmed success | `finalized`, `succeeded`, captured exactly once at the quoted price, including when the client disconnected |
| timeout, crash or ambiguous response after dispatch | `outcome_unknown`; liability held at the quoted amount; no re-execution; reconciled by provider operation key |
| retry, or lost capture response | the original invocation is returned; capture is resolved by `status` on the same key; never a fresh charge |
| settlement fails after successful execution | `finalized` with `settlement_status: pending` or `failed`; only settlement is retried; never executed again |
| late provider result after `finalized` | audited; the charge is unchanged |
| paid stream or long-running task | refused before acceptance (`unsupported-execution`) |

**Deadlines.** Every reservation carries a `reservation_deadline`, 15
minutes after reservation by default. Every non-terminal invocation carries
a `reconciliation_deadline`, 24 hours by default. Crossing it raises an
operator-visible alert (C5). The expiry of a lease or a deadline **proves
nothing** about what the provider did. Unknown liability never becomes
spendable again just because time passed.

**Recovery ownership.**

| State | Who resolves it | How |
|---|---|---|
| `accepted` / `reserved` with no dispatch intent | mctl-api reconciler | release once `reservation_deadline` passes |
| `dispatching` | the dispatcher holding the fencing token; after its lease, the mctl-api reconciler | `outcome(key)` on the provider operation key; a new dispatcher never re-dispatches without a definitive `not_found` after `dispatch_deadline` |
| `dispatched` / `outcome_unknown` | mctl-api reconciler | `outcome(key)` until definitive; then capture or release |
| `payment_status: unknown`, `settlement_status: pending` | mctl-api reconciler | `PaymentAuthorizer.status` on the same key |
| past `reconciliation_deadline` | an operator | an audited, append-only `ResolutionRecord`: actor `prn_…`, evidence reference, decision (`capture` or `release`). This needs the platform-admin role; a tenant cannot resolve its own liability |

### 12. Discovery and read contract

**Descriptor extension (ADR 017).** `CapabilityDescriptor` gains one
optional field, `commerce`. When present it is a summary for the model and
the user, and execution never reads it:

| Field | Meaning |
|---|---|
| `terms_id`, `terms_version` | the current terms version |
| `price_mode` | `free \| per_call \| per_successful_call` |
| `price` | money, sec. 2 |
| `funds_mode` | `simulated \| live` |
| `meter` | `invocation` |
| `quote_required` | always `true` for a paid mode |
| `billable_on` | `accepted_dispatch \| confirmed_success \| never` |

- A descriptor without `commerce` is a **legacy** capability. Its behaviour
  is unchanged, and no ledger row, quote or reservation is created for it.
- A descriptor whose `commerce` references terms that mctl-api cannot
  resolve is **not free**. Its invocation is refused with
  `commerce-terms-unknown`.
- The field is part of the sealed `content_hash`. Sealed sets created before
  this extension keep their hashes because the field is absent. ADR 017's
  "no allow/deny/permit/grant/authorized field" test applies to it unchanged.
- `capability_search` rows may show `price_mode` and `price`.
  `capability_describe` shows the full summary. Both draw only from the
  sealed set, so a capability the caller cannot see shows no price either.

**Tenant reads (C2/C3/C5).** All of these are tenant-scoped and require a
resolved principal with tenant authority. The milestone rule is tenant
admin; mctl-api#489 decides the final roles. A resource in another tenant
returns the same `not-found` as a resource that does not exist.

- the invocation receipt: ids, terms version, quote digest, the five facts,
  `phase`, `reason_code`, timestamps and the `approval_id` /
  `execution_id` / policy-decision references;
- budget: `available`, `reserved`, `committed` and `pending_liability` in
  the tenant currency and `funds_mode`;
- the invocation list, filtered by capability, phase or period.

No read returns arguments, results, payment proofs, `external_ref`
contents, or another tenant's balance.

### 13. Reason codes and audit

ADR 017 sec. 7's closed vocabulary is extended, additively, with:
`identity-unresolved`, `commerce-terms-unknown`, `quote-expired`,
`quote-stale`, `quote-mismatch`, `idempotency-conflict`,
`currency-mismatch`, `funds-mode-unavailable`, `budget-exhausted`,
`payment-denied`, `unsupported-execution`, `outcome-unknown`.
`policy-denied`, `not-eligible`, `not-found`, `provider-unavailable`,
`provider-error` and `timeout` keep their ADR 017 meanings.

Every phase transition, refusal, resolution and late result writes an audit
event that carries ids, digests, money and reason codes, never arguments,
results or proofs. The ledger is the source of truth: losing telemetry,
hitting trace retention or sampling spans loses no charge. Following ADR
012, monetary amounts are not exported as span attributes.

### 14. Storage boundary and compatibility

- **mctl-api owns all commerce state**, as sibling tables in its existing
  Postgres (C2): terms, quotes, invocations, transition history,
  reservations (C3), authorizer records, resolutions and refunds. This is
  the same reasoning ADR 012 used for usage. There is no new service, and
  mctl-agents stores none of it.
- **Envelope versioning.** Every record carries `schema_version: 1`. A
  consumer refuses a version it does not understand.
- **Backward compatibility.** Everything is additive. ADR 012's ledger,
  `internal/usage`, action approvals and execution requests keep their
  schemas and behaviour. Legacy capabilities are untouched.
- **Rollout.** Commerce stays disabled until C5 delivers its evidence and
  rollout procedure. Turning commerce off stops new paid acceptances while
  reconciliation keeps running, so outstanding liability still resolves.
  Merging this ADR enables nothing.

## Testable invariants

C2–C5 must each prove the invariants they touch with a test. Section numbers
point to the rule.

1. Both implementations reproduce the golden `quote_digest` (sec. 5).
2. No monetary field anywhere in the commerce schema has a float type
   (sec. 2).
3. A quote, reservation or capture in a currency other than the tenant's is
   refused with `currency-mismatch` (sec. 2).
4. `funds_mode: live` is refused everywhere in the milestone (sec. 2).
5. A request without a resolved principal is refused at every commerce
   endpoint, even though phase 1 identity resolution lets it through
   elsewhere (sec. 3).
6. Publishing a price change creates a new `terms_version`, and every
   existing quote and invocation keeps its original amount (sec. 4).
7. A descriptor whose commerce terms are unknown is refused, never invoked
   for free (sec. 12).
8. Same key and same digest return the original invocation with no second
   reservation, dispatch or capture. Same key and a changed digest are
   refused with 409 and leave the original unchanged (sec. 6).
9. An approval bound to one quote cannot be consumed by an invocation on a
   different quote (sec. 8).
10. No funds are held while an approval is pending (sec. 7).
11. With EUR 0.12 `per_successful_call` and a EUR 1.00 budget, at most 8
    concurrent invocations capture and the 9th is `budget-exhausted`
    (sec. 7; demonstrated in C5).
12. Each case in the section 11 table ends in exactly the state the table
    gives, including crash before and after dispatch, disconnect, late
    result and unknown authorization or capture.
13. An expired lease or deadline never releases `outcome_unknown` liability
    (sec. 11).
14. A call to a paid provider without a valid mctl-api invocation
    authorization is refused at the provider (sec. 9).
15. A payment proof alone never produces an invocation (sec. 10).
16. Removing telemetry export changes no ledger total (sec. 13).
17. Tenant A cannot distinguish tenant B's invocation or budget from one
    that does not exist (sec. 12).

## Alternatives

1. **Price in MCP annotations or catalog metadata.** Rejected. Annotations
   are advisory and can be set by any provider (ADR 017 sec. 8), and a
   catalog is a view. Either would let discovery decide a charge.
2. **Extend ADR 012's usage record with a customer charge.** Rejected. Its
   dedupe grain (session/result/model) does not identify a tool call, its
   amounts are floats, and mixing customer spend into platform expense is
   exactly what section 1 forbids.
3. **A separate billing service.** Rejected for the same reasons ADR 012
   rejected a separate usage store: it would re-derive identity, duplicate
   authentication and need reconciliation against mctl-api.
4. **Bind the quote by adding an approval intent field (`mctl-action-intent/v2`).**
   Rejected for now. `artifact_hash` already exists for an optional payload
   hash and binds the quote with no encoding change. A version bump is only
   warranted if a later scope needs to bind something it cannot carry.
5. **Adopt x402 or MPP state machines as the core model.** Rejected. Their
   lifecycles differ by scheme and facilitator (see the plan's protocol
   research). The core keeps one authorize/capture/void/status contract, and
   each rail is an adapter behind feature negotiation (C6).

## Non-goals

Real stored value, a wallet product, real-money top-up, provider payouts or
revenue sharing, marketplace UI (mctl-api#23 stays separate), tax/VAT,
subscriptions, FX, auctions, outcome-based pricing, smart contracts,
production x402/MPP, paid streaming or long-running execution, provider
self-service publication of terms, and any production enablement by merging
this document.

## Implementation map

This PR changes only `docs/adr/020-capability-commerce-contract.md`.

| Item | Builds | Depends on |
|---|---|---|
| C2 mctl-api#547 | terms, quotes, invocations, history, idempotency, receipts (secs. 4–6, 11, 13, 14); Go golden-vector test | this ADR |
| C3 mctl-api#548 | reservations, simulated `PaymentAuthorizer`, budget reads (secs. 7, 10, 12) | C2 |
| C4 mctl-agents#619 | descriptor `commerce` field, gateway steps 2–3 and 7–9, provider ingress verification, Python golden-vector test, fault tests (secs. 8, 9, 12) | C3, #197, #198, mctl-api#366 |
| C5 mctl-agents#620 | discovery reads, the EUR 0.12 / EUR 1.00 demo, rollout and disable procedure (secs. 12, 14) | C4, mctl-api#377, #489 |
| C6 mctl-api#549 (optional) | x402 / MPP adapter evaluation against section 10 | C5 |

A follow-up may add implementation detail, but must not reopen without an
amendment to this ADR: the five-fact separation (sec. 1), integer money and
`funds_mode` (sec. 2), server-side identity binding (sec. 3), terms
immutability (sec. 4), the quote encoding and golden vector (sec. 5), the
idempotency scope and derived keys (sec. 6), the approval binding (sec. 8),
the provider outcome rules (sec. 9), "payment proof is never authorization"
(sec. 10), or "time never releases unknown liability" (sec. 11).
