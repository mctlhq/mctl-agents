# Spike #661: canonical capability specs, authoring and maintenance cost

Follow-up 4 of #655 (`docs/spikes/655-openspec-contract.md`). Grammar is the
one verified in #659 (`docs/spikes/659-openspec-grammar.md`). Nothing in a
production module, a `.status.yaml` or an `agents-state` proposal is changed.

## What was built

- Two canonical capability specs, as the code stands on `main` at `ec012ce`:
  - `docs/spikes/661/specs/devloop-approval/spec.md`: 10 requirements, 24 scenarios;
  - `docs/spikes/661/specs/shepherd-merge-gate/spec.md`: 11 requirements, 24 scenarios.
- Citations: every requirement names the code that enforces it, and every
  scenario names the test that pins it.
- A replayed history per capability, `docs/spikes/661/history/<cap>/`:
  - a baseline where one existed: for `devloop-approval`, the state after
    gitops#986 on 2026-09-04;
  - one delta per behaviour change since, in date order. That is 13 deltas.
    7 come from merged agents-state proposals; 6 come from changes that never
    had a proposal.
- `tests/test_sdd_capabilities_661.py` checks three things:
  - replaying every delta with `archive_change` lands on exactly the
    canonical spec;
  - the canonical specs validate;
  - every cited file, symbol and test exists.
- The test is proven both ways. Renaming one cited test fails the citation
  check and the replay. Renaming one requirement in a delta fails the replay.

The historical proposals:

| Capability | Change | Source |
|---|---|---|
| devloop-approval | #349 blocked marker and write guard | proposal `issue-349-…` |
| devloop-approval | #412 implement sweep / execution authorization | proposal `issue-412-…` |
| devloop-approval | #420 bounded approval park | proposal `issue-420-…` |
| devloop-approval | #636 canonical `aar_` grant (PR #651) | no proposal |
| devloop-approval | #658 approval content digest (PR #675) | no proposal |
| shepherd-merge-gate | tier 3 shepherd | proposal `tier3-pr-shepherd` |
| shepherd-merge-gate | settle window (5d53952) | no proposal |
| shepherd-merge-gate | #292 fix-only and `NEVER_MERGE_SERVICES` | proposal `issue-292-…` |
| shepherd-merge-gate | head verdict, re-anchored findings (9e69227) | no proposal |
| shepherd-merge-gate | #411 required checks | proposal `issue-411-…` |
| shepherd-merge-gate | #519 gated merge | proposal `issue-519-…` |
| shepherd-merge-gate | agent-definition gate (6c6a08f) | no proposal |
| shepherd-merge-gate | #633 `UNKNOWN` merge state (ff2bd0f) | no proposal |

## Numbers

```text
capability identification (7 historical proposals, 113 EARS criteria):
  primary capability obvious:          7 / 7 proposals
  proposals within one capability:     0 / 7
  criteria in the two capabilities:    51
  criteria ambiguous between them and another capability: 7
  criteria belonging to other capabilities: 55
    (implementer run outcome, implement sweep, DevLoop merge watch,
     review remediation, shepherd configuration/CLI, CI check reading,
     execution evidence, operator tooling)

authoring:
  requirements:                 21 (10 + 11), 48 scenarios
  requirements mapping cleanly to code/tests: 13
  interpreted_requirements:     8
  agent elapsed time:           ~7 min for both specs, 1 baseline,
                                13 deltas and the test (20:40Z-20:47Z,
                                reading phase excluded);
                                human time was not measured

historical proposals:
  converted:                    7 proposals + 6 changes with no proposal = 13 deltas
  archive cleanly in replay:    13 / 13
  clean_delta:                  11
  delta needing a workaround:   2

size (bytes; tokens ~ bytes / 4):
  current requirements.md, mean of the 7:    9.2 KB  (~2.3k tokens)
  current triplet, mean of the 7:           36.6 KB  (~9.2k tokens)
  delta for the same proposal, mean of 7:    1.9 KB  (~0.5k tokens)
  canonical spec:                       11.8 / 10.5 KB (~3.0k / ~2.6k tokens)
  canonical_spec_plus_delta per change:     ~3.3k tokens, on top of the
                                            triplet, which is still needed

maintenance:
  behaviour changes in the two capabilities:    13
  changes_requiring_spec_update:                13 (every one)
  of those with an agents-state proposal:       7
  of those with no proposal at all:             6 (46%)
  spec drift already observed:                  1 (see below)
  extra_work per change:                        one delta of 1-4 KB; a
                                                few agent minutes; human
                                                time not measured

recommendation: ADAPTER_ONLY
```

### Interpreted requirements

These 8 could not be written straight from a proposal's criteria or a test:

1. **Execution needs a human authorization.** The rule comes from a later
   product decision committed as `0725a2c`, not from #412's acceptance
   criteria.
2. **Approval park is bounded.** Two eras folded into one statement: the
   14-day legacy park (#420) and the shorter canonical park (#636).
3. **Approve signal bound to one workflow.** Carried over from the #655
   fixture. Its scenario cites a test file, not one named test.
4. **Merge waits for a reviewed head.** Assigning the draft rule to the
   tier 3 proposal is approximate.
5. **Merge follows the head's own verdict.** No proposal exists; written from
   the code's comments.
6. **Agent-definition changes need a human merge.** No proposal exists;
   written from the code.
7. **Required checks gate the merge.** 7 criteria of #411 plus the 4
   ambiguous ones condensed into one requirement.
8. **Gated merges go through a single-use approval.** 16 criteria of #519
   condensed into one requirement.

### The two workarounds

1. **#636 could not be a MODIFIED delta.** The canonical grant replaces the
   legacy "approve operation names the approver" behaviour, and its scenarios
   change. Upstream's archive refuses a MODIFIED delta that drops a scenario.
   So the delta is REMOVED plus ADDED, and the spec no longer records that
   one requirement became the other.
2. **#420 needed a canonical-spec edit.** It kept the scenario "Approve
   releases the park" only because that scenario was added to the canonical
   spec. Otherwise the same rule refuses the delta.

### Drift is already real

The #655 fixture `tests/fixtures/sdd/capabilities/devloop-approval/spec.md`
was written on 2026-10-16 and is false on 2026-10-17. Its requirement
"Legacy manual approval flip" says a manual edit to `accepted` lets the
implementer run. Since #349 and #658 such a proposal is refused: it has no
named approver and no content digest. #658 was a hand-written PR with no
proposal and no spec delta, and nothing noticed. That is the six-in-thirteen
case above.

## Evidence for and against ADOPT_CONTRACT

For:

- The grammar and the tooling are not the obstacle. 13 / 13 historical
  deltas, 6 of them for changes that never had a proposal, archive cleanly
  and replay to the exact canonical text. The two workarounds are upstream's
  scenario-loss rule doing its job.
- Writing a spec is cheap for an agent: minutes, not hours.
- Requirement-to-code-to-test citations are a useful index. The citation
  check turns red on a renamed test, which nothing else in the repo does.

Against, and decisive:

1. **No proposal fits one capability.** None of the 7 does. Fewer than half
   their criteria (51 of 113) belong to the two capabilities, and the rest
   spread over at least eight more. To express even one historical proposal
   completely, the catalogue must already cover roughly ten capabilities. The
   investigator would face the cross-capability choice on every proposal,
   not occasionally.
2. **Nearly half the behaviour changes never pass through a proposal.** 6 of
   13 were hand-written PRs. Canonical specs stay true only if every PR
   touching a capability also edits its spec. That is a new merge gate on
   every PR in the repo, not a DevLoop change, and the fixture drifted within
   a day without it.
3. **Specs are lossy, so adopting them creates a second requirements
   source.** 51 criteria became 21 requirements. Details such as #349's
   remedy text and #519's receipt invariants survive only in the proposal.
   The triplet therefore stays the source the implementer needs, and a
   canonical spec next to it is the "duplicate canonical requirements source"
   the invariants forbid.
4. **Context grows, it does not shrink.** About +3.3k tokens per change on
   top of a ~9.2k-token triplet, because the spec replaces nothing the
   implementer reads today.

**Decision input for #655: keep `ADAPTER_ONLY`.**

Revisit only if both of these become true:

- a capability catalogue covers at least 80% of the criteria of the last N
  proposals;
- CI requires a spec delta on any PR that touches a catalogued capability's
  code.

Without the second, the first decays as fast as the fixture did.

## What this does not show

- **Human time.** All times are agent elapsed time in one session.
- **Accuracy of attribution.** Requirement-to-change attribution is by
  first introduction and approximate where noted. It was not traced line by
  line through `git log -L`.
- **Coverage of the repo.** Two capabilities out of roughly ten touched by
  the sample. The 46% no-proposal share comes from 13 changes in these two
  capabilities, not from the whole repository.
