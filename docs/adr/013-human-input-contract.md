# ADR 013 — Human-input contract (durable agent clarification)

- Status: accepted (mctlhq/mctl-agents#333)
- Owner: `orchestrator/human_input.py` (contract), `DevLoopWorkflow` (consumer)
- Producer: the investigator's agent-container side — not built yet, lands
  with mctl-gitops#1277. Until then the request path is only ever read.

## Decision

An investigator that cannot proceed without a human decision writes ONE
sealed `request.json` under its proposal's `human-input/` directory in
mctl-gitops main. The dev loop reads it back (`find_human_input_request`,
a contents-API read — the "Transport" open question is settled the same
way `find_proposal_slug` settled it), parks durably in `WAITING_FOR_INPUT`,
and resumes the investigator with the accepted answers when a response
signal validates.

## Contract invariants

- **Content-addressed and sealed.** `request_hash` is computed over the
  canonical content payload (`_content_payload`), `request_id` is derived
  from it (`"hir-" + request_hash[7:23]`), and `question_hash` binds the
  question to its `ResponseSpec`. `validate()` recomputes all three on
  read; a tampered or carried-over value is rejected. `created_at` is
  deliberately NOT hash-covered: it is metadata, and the consumer bounds
  every wait against its own clock rather than trusting it.
- **Bounded waits.** `expires_at` must parse, exceed `created_at`, and stay
  within `MAX_REQUEST_TTL_SECONDS` of it — and the consumer additionally
  clamps the effective deadline to its own `now + MAX_REQUEST_TTL_SECONDS`
  (`HumanInputState.effective_deadline`), so a model-written far-future
  timestamp cannot park a loop past the TTL.
- **Bounded rounds.** `round` is producer-written and advisory; the
  workflow's own `_human_input_resume_count` is the enforced bound
  (`MAX_CLARIFICATION_ROUNDS`).
- **Answerable by construction.** `requested_from.actor_refs` must name at
  least one actor; `validate_response` checks respondent membership,
  request identity (id + hash) and expiry.
- **Clarification is not approval.** An answer never touches `_approved`;
  the two durable gates (`WAITING_FOR_INPUT` vs `WAITING_FOR_APPROVAL`)
  stay distinguishable by state string.
- **Retirement is by read, never by a workflow-side gitops write.** An
  expired leftover, a document sealed by a foreign workflow id, a stamped
  run id that is not this run's, or a `created_at` predating this
  execution's start (minus `HUMAN_INPUT_PRIOR_RUN_SLACK`) is skipped as
  another execution's leftover. A re-asking investigator overwrites
  `request.json`; the durable answered-marker (producer-side) is tracked
  in mctlhq/mctl-agents#451.
- **No transcripts.** Logs and the `human_input_state` query expose
  hashes, ids, timestamps and counters — never `question`, `reason` or
  `value`.

## Continuation

Accepted answers accumulate; every continuation investigate submit carries
the full set as the `human_input_responses` JSON array (request_id,
request_hash, value, respondent, surface, received_at per entry), so a
later round never has to re-ask an earlier question. Pinning that
parameter against the CWFT definition is producer-side work (#451).

## Numbering note

011 was already used by three sibling contracts (execution-budget,
execution-identity, work-item-resume) before this document was written;
human-input takes 013, the next free number after 012.
