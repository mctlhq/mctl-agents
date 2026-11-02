# ADR 025 — Independent review receipts

> **Status:** proposed
> **Date:** 2026-11-01
> **Issue:** mctlhq/mctl-agents#905 (epic: mctlhq/.github#281, graph: mctlhq/.github#282)
> **Implements (Tier A, pure):** `orchestrator/review_receipts.py`
> **Amended:** mctlhq/mctl-agents#977 (execution-id grammar; Tier B change in the linked mctl-api PR)
> **Pinned by:** `tests/test_review_receipts.py`, `tests/fixtures/review/spike_digest_vectors.json`, `tests/fixtures/review/review_receipt_vectors.json`

## Decision

A review is a sealed receipt bound to the exact bytes and Git SHAs it was issued for. Anything that changes
those bytes or SHAs makes it `stale`. Evidence that was not observed is never `PASS`. Receipts live only in
mctl-api Tier B (private, durable, authenticated); never in public gitops, GitHub reviews or comments.

Nothing consumes the gate yet. `run_shepherd` merge gating (`GATING_BOTS`, `VERDICT_STATES`) is unchanged;
wiring the gate into shepherd or DevLoop is a separately approved slice.

## Documents

`ReviewRequest` (`rr_` + 16 hex) and `ReviewReceipt` (`rc_` + 16 hex), `api_version: review.mctl.ai/v1alpha1`,
sealed like ADR 018: canonical JSON via `context_snapshot.canonical_json`, `content_hash` = `hash_bytes`, id =
prefix + `content_hash[7:23]`, unknown keys rejected, strings bounded, `created_at` excluded from the hash.

- `outcome` is one of `PASS`, `CHANGES_REQUESTED`, `NEEDS_HUMAN`, `UNOBSERVABLE`.
- Criteria use the closed ids of the rubric the subject kind selects: `spike_v1` for `spike_report`
  (`hypothesis_stated`, `method_described`, `measurements_present`, `measurements_reproducible`,
  `claims_traceable_to_measurements`, `limitations_stated`), `proposal_v1` for `proposal` (`scope_bounded`,
  `contracts_specified`, `acceptance_testable`, `rollback_defined`, `matches_issue`).
- `PASS` needs every rubric criterion present and `met`. An `unobserved` criterion forces `UNOBSERVABLE` or
  `NEEDS_HUMAN`.
- `mode: manual` forces `assurance: advisory`; `mode: worker` is `independent`.
- `findings_ref` is an `ArtifactRef`-shaped hash pointer to private findings, never inline text.
- `reviewer_actor`, `reviewer_execution_id` and `assurance` are authoritative only as filled by the Tier B
  server. Tier A validates shape.

### Execution ids

`authoring_execution_id`, when non-empty, must full-match exactly one of:

- `we_<uuid>`: `we_` + lowercase canonical 8-4-4-4-12 hyphenated UUID, a work-item store execution as mctl-api
  mints it (`[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}`);
- `we_[0-9a-f]{16,64}`: legacy hex form, kept unchanged;
- `ex-[0-9a-f]{16}`: runtime execution identity (ADR 011, `execution_identity.seal()`).

Ids are never normalised and are compared byte-exact in the gate, so uppercase, braced, `urn:uuid:`-prefixed or
misplaced-hyphen UUID text is refused with `review_malformed`. Tier A (`_EXECUTION_ID_RE`) and Tier B
(`executionIDRE` in mctl-api) change only together; the shared vectors in `review_receipt_vectors.json` prove it.

## Subjects and digests

- `proposal`: `service`, `slug`, `gitops_commit`, `content_digest`, optional `approval_ref` (`aar_`). The
  digest is `proposal_state.content_digest_of` over `PROPOSAL_DIGEST_FILES` (remote:
  `read_proposal_content_digest`); there is no second proposal digest. `gitops_commit` and `approval_ref` are
  provenance: equal bytes at a later commit are the same proposal (supersession scope of #701; approval refusals
  #702 and execution authorization #703 are untouched).
- `spike_report`: `repository`, `report_root`, `report_path`, full `source_sha`, `base_sha`, `head_sha`,
  `inventory_digest`, `digest_version`. The digest does not fix a report file name.

`spike_report_digest_v1` is SHA-256, rendered `sha256:<hex>`, over:

```
spike_report_digest_v1\n
for each file, sorted by UTF-8 bytes of its report_root-relative POSIX path:
    <len(path bytes)>\n<path>\n<len(content)>\n<content>
<count>\n
```

The files are `report_path` plus every regular file under `measurements/`, all read at one full commit SHA.
The length prefixes keep moving bytes between two files from colliding. Refused with `SpikeDigestError`
(`spike_digest_refused`), never hashed partially: symlink (`120000`), submodule (`160000`) or any non-regular
mode; `..`, `.`, empty segments, backslash, NUL, leading `/`, non-UTF-8 names; case-fold collisions; a path
outside the report and `measurements/`; a missing report; more than `MAX_SPIKE_FILES` (512) files or
`MAX_SPIKE_BYTES` (32 MiB); a truncated tree listing; a non-inlined or oversize blob; blob bytes whose git
blob sha differs; any non-200 read.

## Gate

`evaluate_review_gate(live, request, receipts, required_assurance)`. There is no GitHub review, `commit_id`,
dismissal or check input. Order:

1. `live` unobserved -> `unknown`.
2. Receipts for the same subject identity with the exact binding form the pool (spike: every subject field;
   proposal: `service`, `slug`, `content_digest`). Empty pool: `stale` if other bindings exist, else `missing`.
3. No `request`, an unsealed request, a request bound to a different revision than `live` (exact binding, not
   just identity), or a worker request without `authoring_execution_id` -> `unknown`. Actor inequality alone
   never proves independence, because agent commits carry the operator's identity.
4. Keep only receipts whose `request_id` is the request's own; none left -> `missing`.
5. Drop receipts whose reviewer actor is empty or matches an author actor (case-insensitive), whose execution
   equals the authoring execution, whose `attempt_id` is an authoring attempt, or whose `lineage_root` equals
   the authoring one. A worker receipt with an empty `reviewer_execution_id`, `attempt_id` or `lineage_root`
   is dropped too: an unobserved identity is not independence. Nothing left -> `not_independent`.
6. `required_assurance=independent` and only advisory receipts left -> `advisory_only`.
7. The latest remaining receipt (Tier B ingest order, oldest first): `satisfied`, `changes_requested`,
   `needs_human` or `unobservable`.

Reviewer identity is never inferred from a model name, a pasted string or an OAuth client name (#891).

## Tier B adapter (mctl-api, separate repository and PR)

Required before any live-acceptance claim; deployed mctl-api was not observed to have it. New append-only
resource, referencing `ExecutionEvidence` by `ev-` id and `ArtifactRef(kind="review-receipt")`:
`POST /api/v1/review-requests`, `POST /api/v1/review-receipts` (worker: enrolled
`service:mctl-agents-reviewer`, request id, single-use nonce, attempt id, ADR 010 fencing epoch; manual:
direct human principal via the existing `isHumanAdmin` / `CheckHumanPrincipal` check, stored `manual` /
`advisory`), `GET /api/v1/review-receipts` (complete pool or typed 5xx), MCP `mctl_submit_review_assertion` and
`mctl_get_review_gate`. Tenant and actor are always server-derived. Typed refusals, nothing stored:
`review_nonce_replayed`, `review_request_closed`, `review_subject_mismatch`, `review_producer_not_enrolled`,
`review_fence_stale`, `review_pass_with_unobserved`. Tier A codes: `review_rubric_mismatch`,
`review_unobserved_outcome`, `review_malformed`, `review_unknown_key`, `review_bad_subject`,
`review_bad_identity`, `spike_digest_refused`.

## Reviewer capabilities

The `independent-review` execution profile grants read-only subject inputs and receipt submission only: no
GitHub write, merge, label, lifecycle approval or approval signal; the policy checkpoint (ADR 014) refuses
`github_write`. The reviewer never posts as `chatgpt-codex-connector[bot]` or any `GATING_BOTS` login.

## Non-goals

Implementation approval; reusable-review dedup (.github#283); inline-comment re-anchoring (#336); same-head
finding dispute (mctl-gitops#1176); OAuth model inference (#891); changing `run_shepherd`; the explainer
(#670/#846/#847), which stays advisory.
