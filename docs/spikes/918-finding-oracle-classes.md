# Spike #918: which DevLoop finding classes have a deterministic oracle

STATUS: DRAFT

Refs #918. Docs, fixtures and an offline harness only. Nothing is wired into DevLoop. The owner records
`decision.verdict`.

## Sources and limits

| source | status |
|---|---|
| `docs/adr/018-execution-evidence-envelope-contract.md` (`AUTHORITIES` in `orchestrator/execution_evidence.py`) | read (symbol check) |
| `docs/adr/022-runtime-assurance-contract.md` (`transition_finding` in `orchestrator/runtime_assurance.py`) | read (symbol check) |
| `docs/adr/024-security-finding-remediation-runner.md` (`orchestrator/security_findings/`) | not-observed in depth; vocabulary taken from the proposal |
| `SpecVerdict` in `orchestrator/sdd/verify.py` | read |
| `CheckBlocker` in `orchestrator/ci_checks.py` | read |
| `CodexFinding` in `orchestrator/run_shepherd.py` | read |
| `orchestrator/source_issue.py`, `orchestrator/spike_issue.py` | not-observed |
| mctlhq/.github#222 (affectedness pattern) | not-observed; the lockfile check is the harness's own |
| mctl-gitops `agents-state/mctl-agents/proposals/*/.status.yaml` | read for 6 proposals (198, 199, 239, 240, 746, 875) |
| shepherd P1/P2 review comments, CI check runs | not-observed: no network in the implementer run |

Limits: the corpus has 16 findings, not 60. Real findings come from `.status.yaml` and this repo; 6 of 16 are
hand-built controls and are marked `origin: control`. No shepherd comments or CI check runs were captured, so
the runtime/test class is exercised only with controls and a missing capture.

## Taxonomy and oracle map

| class | oracle | existing_oracle | evidence_consumed | binding | falsity_provable | gate_allowed |
|---|---|---|---|---|---|---|
| runtime/test failure | BOUNDED_BUT_NONDETERMINISTIC | `ci_checks.CIStatus` / `CheckBlocker` | check-run conclusions and reruns at head SHA | head SHA | no: a pass does not disprove a flaky failure | yes, when reruns agree |
| static invariant / schema | DETERMINISTIC | `sdd/verify.py` `SpecVerdict`, `orchestrator/validate_manifest.py` | file and symbol listing | revision | no: absence of a symbol only means not reproduced | yes |
| security with safe local oracle | NO_SAFE_ORACLE | `security_findings.verification` (ADR 024) when a mechanism is bound, else none | none captured in this spike | exact revision | no | no |
| dependency / version affectedness | DETERMINISTIC | `uv.lock` excerpt against a declared range | lockfile versions | revision + advisory id | no | yes |
| stale factual premise | DETERMINISTIC | GitHub state capture; file existence at the pinned SHA | issue/PR state, file listing | SHA or capture time | yes: canonical state field is complete | yes for GitHub and repo state; no for live cluster state |
| architecture / design | NO_SAFE_ORACLE | none | none | none | no | no |
| governance / ownership | NO_SAFE_ORACLE | none for the judgement part; lifecycle ownership store read for the mechanical part | none captured | capture time | no | no |
| incomplete requirement / ambiguous decision | NO_SAFE_ORACLE | none; route to `orchestrator/human_input.py` (ADR 013) | none | none | no | no |

Deviations from the proposal's provisional ratings: security is rated NO_SAFE_ORACLE here because no ADR 024
mechanism was captured; governance is split only in prose, and its mechanical part was not exercised.

## Experiment

Command: `uv run python tools/spike_918_finding_oracles.py --results` writes `docs/spikes/918/results.json`.
Validators read structured claim fields and captured evidence under `docs/spikes/918/evidence/`, never the
claim text. No network, no exploit payload, and no output field grants, approves or merges anything.

Outcomes: `VALIDATED_FINDING`, `NOT_REPRODUCED`, `FALSE`, `UNKNOWN`, `HYPOTHESIS`. They stay distinct in every
record and metric. `FALSE` is emitted only for GitHub state checks (see `falsity_provable`).

### Metrics

Before arm: every finding actionable. After arm: only `VALIDATED_FINDING` actionable.

| metric | value |
|---|---|
| corpus size | 16 (limit 60) |
| validator-eligible findings | 12 |
| labelled eligible findings | 9 (6 true_positive, 3 noise) |
| precision before / after | 0.667 / 1.0 |
| false-positive rate before / after | 1.0 / 0.0 |
| validator coverage | 0.75 |
| false negatives introduced by gating | 0 |
| NOT_REPRODUCED rate (eligible) | 0.167 |
| UNKNOWN rate (eligible) | 0.25 |
| FALSE rate (eligible) | 0.0 |
| findings that stay HYPOTHESIS by design | 4 |
| added latency per finding | under 1 ms (fixture replay) |
| model cost | 0 USD (no model calls) |
| reviewer effort / time-to-decision | not-measured: no human study and no timestamps captured |

Caveat: n is tiny, controls were built to exercise each outcome, and labels such as "merged" are weak. The
precision and FPR figures show the harness separates cases, not a production rate. The runtime/test noise case
(f05) ended `UNKNOWN`, not rejected, because its check run was not captured.

## Status/evidence contract (mapping only)

| outcome | ADR 018 | ADR 022 | ADR 024 | SpecVerdict |
|---|---|---|---|---|
| VALIDATED_FINDING | `observed` evidence with an `ev-` id | `confirmed` | `passed`/`failed` per direction | `fail` |
| NOT_REPRODUCED | gap | `rejected` + `not_reproducible` | none | none |
| FALSE | gap | `rejected` + `false_positive` | none | none |
| UNKNOWN | none | none | `unavailable`/`error`/`timeout` | `unknown` |
| HYPOTHESIS | `asserted` | `candidate` | none | none |

Gap: investigator and shepherd findings have no record that carries a revision-bound verdict. ADR 018
evidence ids can carry it, so no new record is proposed.

## Classes never gated

These stay visible as `HYPOTHESIS`: architecture/design; governance judgement; incomplete requirement /
ambiguous product decision (routed to the ADR 013 human-input contract, not a validator); any security
finding without a safe local oracle; the live-cluster part of stale-premise claims.

## Invariants

- Validators consume bounded evidence pinned to a revision, digest or capture time, never conclusion text.
- `VALIDATED_FINDING` records the binding it checked.
- `NOT_REPRODUCED` keeps the hypothesis visible; it is not falsity.
- No oracle gives `HYPOTHESIS`; inconclusive evidence gives `UNKNOWN`; neither means false.
- No exploit payloads and no third-party or production targets.
- No validation result grants approval, merge or authorization.

## Recommendation

VALIDATE_SELECTED_FINDING_CLASSES (provisional; STATUS stays DRAFT). The existing vocabularies carry all five
outcomes except that NOT_REPRODUCED and FALSE have no ADR 018 carrier. Evidence is thin; the owner should
decide after a corpus with real shepherd and CI findings.

## Follow-ups (proven gaps only)

1. Label investigator stale-premise claims with a revision-bound oracle result as ADR 018 `observed`
   evidence. Owner repository: mctl-agents. Control: shadow-only labelling, no gating. Rollback: revert the
   labelling change.
2. Define an ADR 018 carrier for `NOT_REPRODUCED`/`FALSE`. Owner repository: mctl-agents. Control: ADR
   amendment reviewed by the owner. Rollback: revert the ADR change.
