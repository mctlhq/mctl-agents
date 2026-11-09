# mctl bridge contract v1alpha2

Data-only, additive contract for the mctl-api command bridge: typed **read** commands and
results, the approval basket and its fingerprint, the Glovo `place_order_fake` write
command, and the claim response envelope. Canonical home: this directory. Consumers
(mctl-api, mctl-telegram) vendor files verbatim, pinned by `SHA256SUMS`, and never edit a
vendored copy. Any change starts here.

**v1alpha1 is unchanged.** `docs/spikes/1043/` stays byte for byte as it was (pinned in
`tests/test_bridge_v1alpha2_contract.py`, T1, by the sha256 values in mctl-api
`internal/bridge/schema/PROVENANCE`: command `b857f62b...6dae1`, result `c91bb86e...9354f`).
v1alpha1 remains the contract for every write except the Glovo `place_order_fake`, and for
`read_status`.

Out of scope: any mctl-api or mctl-telegram code, any HTTP route, any Temporal workflow or
signal, `prepare` (a write in mctl-api, not a read kind), `cancel_order_fake` and every other
write, and real provider access. The Glovo `place_order_fake` is the one v1alpha2 write
(OPEN-1059-1 is closed: it is defined by `command-place-order-fake.schema.json`).

## Files

| File | `$id` suffix | Purpose |
|---|---|---|
| `read-command.schema.json` | `v1alpha2/read-command.schema.json` | `read_history`, `read_preview`, `read_basket` |
| `read-result.schema.json` | `v1alpha2/read-result.schema.json` | typed result with payload and digest |
| `approval-basket.schema.json` | `v1alpha2/approval-basket.schema.json` | what a human approves; the fingerprinted bytes |
| `command-place-order-fake.schema.json` | `v1alpha2/command-place-order-fake.schema.json` | the one v1alpha2 write |
| `claim-response.schema.json` | `v1alpha2/claim-response.schema.json` | versioned claim envelope |
| `fixtures/` | | valid and invalid documents, one reason each |
| `vectors/jcs-digest.json` | | RFC 8785 vectors |
| `vectors/basket-fingerprint.json` | | basket to canonical bytes to fingerprint, plus `must_reject` |
| `vectors/must-reject.json` | | raw text every consumer must reject |
| `vectors/pattern-parity.json` | | strings every consumer must accept or reject per pattern |
| `SHA256SUMS` | | sha256 of every other file here |

Reference implementation (Python, repo only): `tools/jcs.py` (stdlib only),
`tools/bridge_contract.py` (`check_read_command`, `check_read_result`, `check_approval_basket`,
`check_write_command`, `check_claim_response`, all over raw bytes; CLI `validate`, `digest`,
`fingerprint`, `sums`), and `tools/bridge_fixtures.py` (regenerates `fixtures/` and `vectors/`,
`--sizes` prints the table below). `tools/jcs.py` is bridge-contract-only; it is not
`orchestrator.context_snapshot.canonical_json`, which is a different, non-JCS form.

## Read command

`schema_version` is `bridge.mctl.ai/v1alpha2`; `additionalProperties: false`; every field is
required unless marked optional.

| Field | Constraint |
|---|---|
| `command_id` | `^cmd_[A-Za-z0-9]{26}$` |
| `request_id` | `^rdq_[A-Za-z0-9]{26}$`, the read's idempotency key |
| `kind` | `read_history`, `read_preview`, `read_basket` |
| `attempt` | integer 1..100 |
| `tenant_ref`, `principal_ref`, `connection_ref` | v1alpha1 patterns (`tnr_`, `prr_`, `gcn_`) |
| `install_revision_ref` | `^irv_[A-Za-z0-9]{16,40}$` |
| `issuer` / `audience` / `nonce` | const `mctl-api` / `^provider:[a-z0-9-]{1,32}$` / `^[A-Za-z0-9_-]{22}$` |
| `issued_at`, `expires_at` | date-time and an RFC 3339 UTC pattern |
| `ttl_seconds` | integer 1..300 |
| `lease_token` | optional, v1alpha1 pattern |
| `params` | per kind, below |

`execution_id`, `work_item_id`, `order_ref`, `workflow_id`, `approval_ref` and `intent_hash` are
forbidden (`additionalProperties: false` plus an explicit `not/anyOf`): a read happens before
any order or `we_` exists. There is no `cancel_requested`; the provider protocol cannot cancel a
read. Expiry, or a consumer-side cancel before the claim, is the only stop.

Params: history `{limit 1..20, cursor? ^[A-Za-z0-9_-]{1,64}$}`, preview
`{history_order_ref ^pho_[A-Za-z0-9]{16,40}$}`, basket `{}`.

## Read result

`additionalProperties: false`. Required: `schema_version`, `command_id`, `request_id`, `kind`,
`lease_token`, `attempt`, `worker_identity`, `outcome`, `provider_effect` (const `none`),
`completed_at` (RFC 3339 UTC pattern). Optional: `error_code`, `error_detail` (printable ASCII,
1..128 bytes). There is no `tenant_ref`, `principal_ref`, `connection_ref` or
`install_revision_ref`: bindings come only from the consumer's own command row.

`outcome` is `succeeded | failed_retryable | failed_terminal | cancelled`. `payload` and
`payload_digest` are required iff `outcome` is `succeeded` and forbidden otherwise.

- History payload: `orders` 0..20 of `{history_order_ref (pho_), placed_on
  ^[0-9]{4}-[0-9]{2}-[0-9]{2}$, status, total_minor 0..10000000, currency, line_count 1..100,
  lines 1..5 of {item_code, quantity 1..100}}`, optional `next_cursor`. Only the first five lines
  are carried; `line_count` says how many there are.
- Preview payload: required `empty`, `display` (0..30 of `{item_code, item_name}`),
  `unavailable_items` (0..30 `item_code`, disjoint from the basket lines); `approval_basket`
  required iff `empty` is false, forbidden iff it is true.
- Basket payload: required `empty`, `display`; optional `store_name`; `approval_basket` iff
  `empty` is false.
- `item_code` `^FAKE-[A-Z0-9-]{1,32}$`; `item_name` and `store_name`
  `^[A-Za-z0-9 .,'()-]{1,60}$` (no character that JSON escapes, so bytes equal characters).
- An empty read (`empty: true`, `display: []`) is a valid, successful read. It has no approval
  basket, so it can never be fingerprinted, prepared or approved.
- Every `display` `item_code` must be a line of the basket (checked by the reference validator).

### Crosswalk to mctl-api#957

| #957 tool / field | v1alpha2 source |
|---|---|
| `history {limit<=20, cursor?}` | history params |
| history item ref | `history_order_ref` (`pho_`; never `ordr_`, mctl's own order index) |
| history `next_cursor?` | `payload.next_cursor` |
| history item summary | `placed_on`, `status`, `total_minor`, `currency`, `line_count`, `lines` |
| `POST /previews {order_ref}` | preview params `{history_order_ref}`; the #957 wire field stays `order_ref` and carries the `pho_` ref |
| preview `items` | `approval_basket.lines` joined with `display` (empty when `empty` is true) |
| preview / basket `empty` | `empty` (shown, never prepared) |
| preview `unavailable_items` | `unavailable_items` |
| preview `fingerprint` | computed by mctl-api: `basket_fingerprint(approval_basket)` |
| preview `preview_id`, `expires_at` | minted by mctl-api, not from the provider |
| basket `store_name?` | `store_name` |
| basket `totals_label` | formatted by mctl-api from the basket's amounts and `currency` |

## Approval basket and fingerprint

`approval-basket.schema.json`: all fields required, `additionalProperties: false`.

| Field | Constraint |
|---|---|
| `v` | const `mctl-approval-basket/v1` |
| `store_ref` | `^pst_[A-Za-z0-9]{16,40}$` |
| `currency` | `^[A-Z]{3}$` |
| `lines` | 1..30 of `{item_code, quantity 1..100, unit_price_minor 0..1000000}` |
| `subtotal_minor` | 0..3000000000 (`maximum`), `= sum(quantity * unit_price_minor)` |
| `delivery_fee_minor`, `service_fee_minor`, `discount_minor` | 0..10000000 |
| `total_minor` | 0..3020000000 (`maximum`), `= subtotal + delivery_fee + service_fee - discount` |

No display, availability or time field. Rules JSON Schema cannot express, which every consumer
MUST also check: `lines` sorted ascending by `item_code` and `item_code` unique (reject, never
re-sort), and the two identities.

`basket_fingerprint = "sha256:" + lowercase hex(SHA-256(JCS(approval_basket)))`. This is the
single M1 fingerprint. A provider-supplied value is only ever compared. The read
`payload_digest` is a transport-integrity digest and is never an approval fingerprint.

## Write command: `place_order_fake`

`command-place-order-fake.schema.json` is a delta over the v1alpha1 `command.schema.json`: the
same closed property list, and the required list of v1alpha1 plus `approval_ref`.

| Field | v1alpha2 write command |
|---|---|
| `schema_version` | const `bridge.mctl.ai/v1alpha2` |
| `kind` | const `place_order_fake` |
| `approval_ref` | required, `^aar_[A-Za-z0-9]{16,40}$` |
| `payload` | the approval basket exactly (`$ref`), no wrapper and no other top-level field |
| `issued_at`, `expires_at` | date-time and the RFC 3339 UTC pattern |
| `ttl_seconds` | integer 1..900, unchanged |
| `cancel_requested` | required boolean, unchanged |
| `lease_token` | optional, unchanged |
| `install_revision_ref`, `request_id`, `params` | not present, so rejected |
| every other v1alpha1 property | unchanged |

Result: the unchanged v1alpha1 `result.schema.json` (`schema_version` `bridge.mctl.ai/v1alpha1`).
There is no v1alpha2 write result. The read-result schema rejects it, and the v1alpha1 result
schema rejects a v1alpha2-versioned document. mctl-api derives `effect_state` from
`provider_effect` as for v1alpha1.

Ownership: this issue owns the schema, this table and the result rule. mctl-api#986 section 2a
owns the per-command wire version, projection, Claim gating, `SubmitResult` and vendoring.
mctl-telegram#1262 task 2b owns parsing and executing it (fake only).

## Claim response

`claim-response.schema.json`, `additionalProperties: false`. The envelope version is independent
of each command's `schema_version`.

| Field | Constraint |
|---|---|
| `schema_version` | `bridge.mctl.ai/v1alpha1` for a provider that declares only v1alpha1; `bridge.mctl.ai/claim/v1alpha2` for one that declares v1alpha2 |
| `commands` | 0..10 (mctl-api `MaxClaimBatch`); in the v1alpha1 envelope only v1alpha1 commands, in the `claim/v1alpha2` envelope v1alpha1 commands, read commands and the v1alpha2 write |

A provider that declares only v1alpha1 gets the v1alpha1 envelope, the two fields above with
v1alpha1 commands only, unchanged. The fixture `claim-response.v1alpha1-only.valid.json` is that
shape. It was built from the v1alpha1 command fixture, and the mctl-api writer
(`internal/api/handlers_bridge.go`) was not available when this was written, so mctl-api#986
must confirm byte equality in its own drift test.

## Size cap

The transport decides the cap: mctl-api reads at most `bridgeMaxBody = 16384` bytes per result
body. The whole result body is at most **16384** bytes and `JCS(payload)` at most **12288**
bytes; over either is `payload_too_large` in the reference validator (and `invalid_document` at
the mctl-api route). Measured with every field at its maximum
(`python tools/bridge_fixtures.py --sizes`; the maximal fixtures are `fixtures/*.max.valid.json`):

| Payload | Worst case |
|---|---|
| history, 20 orders x 5 lines | 10,673 B |
| basket read, 30 lines | 7,127 B |
| preview, 30 lines and 30 unavailable | 8,273 B |
| approval basket alone, 30 lines | 3,135 B |
| result envelope without payload (every field at its maximum) | 686 B |

An earlier estimate of the envelope was 910 B; the reproducible measurement is 686 B. Either
way no schema-valid result approaches the caps, which are defense in depth against a schema
bypass. `read-result.at-cap.json` is exactly 16384 bytes (valid, padded with JSON whitespace)
and `read-result.over-cap.json` is 16385 bytes (schema-valid, rejected). The transport limit is
not raised.

## Raw bytes, numbers and digest

Consumers validate from raw bytes. The **pre-check** runs before any schema and rejects:
invalid UTF-8; a BOM; a root that is not exactly one JSON object; trailing bytes; raw control
characters in strings; nesting deeper than 32; duplicate keys at any depth, compared after JSON
unescaping (`"a"` and `"\u0061"` are one key); any `\u` escape inside an object key (every
contract key is plain ASCII); lone surrogate escapes, high or low; `NaN`, `Infinity` and
`-Infinity`; and numbers outside +-(2^53-1) (an I-JSON profile rule on top of RFC 8785).

Integer fields must be JSON integer tokens `-?(0|[1-9][0-9]*)`. A token with `.`, `e` or `E`
(`1.0`, `1e2`), or `true`/`false`, in an integer position is rejected; JSON Schema
`"type":"integer"` alone would accept `1.0`. Every number in these contracts is an integer.

`vectors/must-reject.json` holds these as raw text. Every consumer, Go included, MUST reject
every entry with its own token-level check (Go `encoding/json` accepts duplicate keys and
replaces lone surrogates).

`payload_digest = "sha256:" + hex(SHA-256(JCS(payload)))` per RFC 8785, `bool` tested before
`int`. Consumers MUST store exactly `JCS(payload)` as TEXT or BYTEA, never JSONB, and re-verify
`sha256(stored) == payload_digest` on every serve without re-parsing. `vectors/jcs-digest.json`
(24 vectors, `canonical` written by hand from RFC 8785 sections 3.2.2 and 3.2.3 and the ES
number rules) reproduces byte for byte in `tools/jcs.py` and in an independent Go
implementation written from scratch for this check (`encoding/json` with `UseNumber`, UTF-16
key sort, ES number formatting): 24 of 24 matched with go1.26.7 on 2026-11-09. Go consumers run
the same file.

### Pattern semantics

Every `pattern` is in the subset that Go RE2 and Python `re` read identically: literals, `[...]`
classes with explicit ASCII ranges, `{m,n}` repeats, groups, `|`, `?`, and `^`/`$` at the two
ends. Never `\d \w \s \b \Z \z`, lookaround, backreferences or flags. Digits are `[0-9]`. The
Python reference matches with `re.fullmatch` and `re.ASCII`. `vectors/pattern-parity.json` pins
accepted and rejected strings (trailing `\n` and `\r`, Arabic-Indic and fullwidth digits, lowercase
and non-ASCII `item_code`); a lint test fails if a pattern leaves the subset.

## Consumer MUST

A consumer (mctl-api #986, #957, #988, #989) MUST:

1. take tenant, principal, connection and install-revision bindings only from its own stored
   command row; a result that adds any is invalid;
2. require `result.kind == stored command kind`, `command_id` and `request_id` to match, and the
   lease and attempt to be current;
3. reject a result that arrives after the command's `expires_at`;
4. run the raw-bytes pre-check, then the schema, then the size cap, and recompute
   `payload_digest` instead of trusting it;
5. store only `JCS(payload)`, and re-verify the digest on every serve;
6. re-check at serve time: binding, TTL, connection revocation, install revision and the
   `glovo:read` scope;
7. never persist `lease_token`;
8. never write a raw payload to an outbox, signal, audit row, log or Temporal history;
9. compute `basket_fingerprint` itself from the approval basket and only compare any fingerprint
   received from elsewhere;
10. accept a `pho_` ref or a `cursor` in a read command only if a prior history read for the same
    tenant, principal and connection returned it (owned by mctl-api#957).

## Provider MUST

A provider (mctl-telegram#1262 and any later one) MUST:

1. resolve `history_order_ref` and `cursor` only within the command's `connection_ref` and
   `principal_ref`; a ref or cursor it did not issue to that binding, or cannot resolve, is
   `outcome: failed_terminal` with `error_code: provider_rejected`, never data from another
   binding;
2. issue `pho_` refs and cursors that are opaque and unguessable, and never reuse one across
   bindings;
3. emit approval-basket lines sorted and unique, and never put a value it did not read from the
   bound account into a payload.

## Versioning and vendoring

- Each claimed command carries its own `schema_version`; the claim response states the claim
  protocol version.
- mctl-api MUST NOT lease a v1alpha2 command (read or write) to a provider whose server-side
  config does not declare `bridge.mctl.ai/v1alpha2`. The provider is never trusted to filter.
- A provider that receives an unknown `schema_version` drops it without side effects; mctl-api
  expires the command as `expired`.
- Any byte change to a schema is a new `schema_version` and a new `$id`. Nothing is "additive
  within a version". A v1alpha2 command inside a v1alpha1 claim envelope is rejected.
- `SHA256SUMS` lists every published file. Each consumer vendors the subset below, records the
  sha256 of `SHA256SUMS` in its `PROVENANCE`, and its drift test checks every vendored file
  against the matching line. A test here regenerates `SHA256SUMS` in memory and compares it.

| Consumer | Files | Location |
|---|---|---|
| mctl-api#986 task 1 | `read-command`, `read-result`, `command-place-order-fake` and `claim-response` schemas and their fixtures | `internal/bridge/schema/v1alpha2/` |
| mctl-api#988 task 2 and 2a (the one basket copy) | `approval-basket.schema.json`, `basket-fingerprint.json`, `pattern-parity.json`; `jcs-digest.json`, `must-reject.json` | `internal/glovo/basket/contract/`; `internal/jcs/testdata/` |
| mctl-telegram#1262 task 2b | `command-place-order-fake`, `approval-basket` and `claim-response` schemas | its `schema/` directory |

All fixtures and vectors are synthetic.
