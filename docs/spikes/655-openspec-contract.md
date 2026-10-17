# Spike #655: OpenSpec-compatible artifact contract for DevLoop proposals

Compatibility target: OpenSpec **v1.14.2**, commit **`f80d860`** (`f80d860fc510ba4f51180c8ddde35b91b32de657`).
Only facts pinned in mctlhq/mctl-agents#655 are used, plus what the prototype verifies itself.
The prototype is `orchestrator/sdd/` (pure Python, PyYAML only, no Node, no OpenSpec CLI) and is imported by nothing in production.

## Recommendation: `ADAPTER_ONLY`

Keep the MCTL triplet as the authored format and `.status.yaml` as execution state. Maintain a round-trip **adapter**
(`orchestrator/sdd/convert.py`) so an OpenSpec-style change package can be produced from, and read back to, a proposal on demand.
Do not make OpenSpec the authored source now.

Why not `ADOPT_CONTRACT`:
- The spec/delta markdown grammar was **unverified** when this was written. It has since been verified against f80d860 (#659, `docs/spikes/659-openspec-grammar.md`), which removes this reason but not the others; the decision stays `ADAPTER_ONLY` until the authoring-cost evidence of #661 is in.
- Schema commands are experimental at v1.14.2.
- Existing proposals have no capability decomposition; canonical specs would have to be authored before deltas mean anything.
- Adoption touches the investigator's publish guards (`TRIPLET`, `_landed_triplet_defects`, prompts) and context assembly.

Why not `NO_GO`: the round trip on issue-596 is lossless and byte-exact, the delta engine is cheap, and the adapter is fully reversible.

## Mapping table

| Current artifact / section | Candidate target |
|---|---|
| requirements.md title (H1) | `proposal.md` H1, `.openspec.yaml` `goal` |
| requirements.md Context | `proposal.md` |
| requirements.md User stories | `proposal.md` |
| requirements.md Acceptance criteria (EARS) | `specs/<capability>/spec.md` ADDED requirements (one per EARS line, verbatim, plus a derived scenario) |
| requirements.md Out of scope | `proposal.md` |
| requirements.md Open questions | `proposal.md` |
| design.md (Current state / Proposed solution / Alternatives / Platform impact) | `design.md`, verbatim |
| tasks.md tasks / Tests / Rollback | `tasks.md`, verbatim |
| `.status.yaml` `source` (url, repo, issue) | `.openspec.yaml` `x-mctl-issue`, `x-mctl-repo`, `x-mctl-issue-number` |
| service, slug | `affected_areas`, `x-mctl-service`, `x-mctl-slug` |
| everything else in `.status.yaml` | stays MCTL execution state; never copied |

## `.openspec.yaml` field map

- Known keys used: `schema` (required, `mctl-devloop`), `created` (`YYYY-MM-DD`), `goal`, `affected_areas`; `initiative` (`store`, `id`, kebab-case, no `x-` keys), `skip_specs`, `retire_capabilities` where applicable.
- Unknown top-level key: warning, error in strict mode. Top-level `x-` keys pass through unchanged.
- MCTL extensions: `x-mctl-issue`, `x-mctl-repo`, `x-mctl-issue-number`, `x-mctl-service`, `x-mctl-slug`, `x-mctl-acceptance-heading` and `x-mctl-acceptance-after` (round-trip layout), `x-mctl-change-class` (advisory).
- Forbidden, even non-strict, bare or as `x-mctl-<key>`: `status`, `approval`, `control`, `execution`, `attempt`, `pr`, `github`, `merged_at`, `merge_commit`, `context`, `request` (`FORBIDDEN_EXECUTION_KEYS`). Tests T1-T7.

## Loss report and sizes (issue-596)

`loss_report` located all 88 headings, bullets, checkboxes and source values in the package, with zero unmapped. The round trip
`change_to_triplet(triplet_to_change(x))` is byte-exact for all three files and the source block (T10, T11).
Group heading lines inside the acceptance section are carried as `_Group: X_` lines (none occur in issue-596).

| Representation | Bytes | Approx. tokens |
|---|---|---|
| Current triplet | 24,865 | 6,211 |
| Candidate package (`.openspec.yaml` 473, proposal 4,037, design 10,610, tasks 7,577, spec 6,316) | 29,013 | 7,248 |

Token method: `ceil(chars / 4)`, an estimate with no tokenizer. The candidate is about 17% larger because EARS lines gain a derived scenario and a requirement header. Proposals 516 and 519 were not converted.

## Delta semantics: pinned rule vs MCTL choice

Order RENAMED -> REMOVED -> MODIFIED -> ADDED, with cross-section pre-validation. Every abort leaves inputs byte-identical and archive never moves the change. Fixtures: `tests/fixtures/sdd/delta-rows/<row>/`; test `test_t8_delta_row[<row>]`.

| Row | Pinned rule | MCTL choice | Test row |
|---|---|---|---|
| Order | R, R, M, A | mirror archive | `order-r-r-m-a` |
| RENAMED FROM also REMOVED | abort | mirror archive | `rename-from-also-removed` |
| RENAMED TO collides with ADDED | abort | mirror archive | `rename-to-collides-added` |
| RENAMED source missing, target present | no-op | mirror archive | `rename-source-missing-target-present` |
| RENAMED both missing | abort | mirror archive | `rename-both-missing` |
| REMOVED missing | warn, no-op | mirror archive | `removed-missing-warns` |
| REMOVED near-miss | abort | mirror archive | `removed-near-miss` |
| MODIFIED missing | abort | mirror archive | `modified-missing` |
| MODIFIED identical | no-op | mirror archive | `modified-identical-noop` |
| MODIFIED drops scenarios | abort | mirror archive | `modified-drops-scenarios` |
| ADDED identical | no-op | mirror archive | `added-identical-noop` |
| ADDED different | abort | mirror archive | `added-different-abort` |
| ADDED near-miss | abort | mirror archive | `added-near-miss` |
| New capability | ADDED only; M/R abort; REMOVED skipped with warning | mirror archive | `new-cap-*` |
| Duplicate headers in main spec | rejected before apply | mirror archive | `duplicate-headers-main` |
| Idempotency | per requirement; re-archive fails (target exists) | mirror archive | `test_t9_*` |
| ADDED of an existing name | archive aborts; sync = implicit MODIFIED | **MCTL-specific: abort** (silent upgrades hide intent from the approver; DevLoop is fail-closed) | `sync-divergence-*` |

## Canonical sync demonstration (DevLoop approval gate)

`tests/fixtures/sdd/capabilities/devloop-approval/spec.md` (derived from `proposal_state.human_approval_satisfied` and the approval park in `dev_loop.py`) has four requirements.
Change `devloop-approval-content-digest` ADDS "Approval records proposal digest", MODIFIES "Anonymous approver refused" (adds a scenario) and REMOVES "Legacy manual approval flip".
After archive the spec equals `changes/devloop-approval-content-digest/expected/spec.md`, and the change moves to `archive/YYYY-MM-DD-<change>` exactly once (T9).

## Investigator -> artifact -> Implementer (simulated, issue-596)

No real implementer run was made. "Implementation success" is a reachability check: every task and every EARS line of issue-596 is present in the package (empty loss report). The issue-655 triplet itself was not converted.

| Dimension | Verdict | Evidence |
|---|---|---|
| Requirement clarity | improve | one requirement plus scenario per EARS line, addressable by name |
| Task completeness | preserve | tasks.md and design.md byte-identical |
| Context size | worsen | about +17% bytes/tokens on issue-596 |
| Ambiguity | preserve | EARS text carried verbatim; scenarios are derived, not authoritative |
| Implementation success | preserve (simulated) | all tasks and criteria reachable |
| Changed-capability identification | improve | `specs/<capability>/`; but the investigator must now choose a capability |

## Shepherd boundary

Spec verification is a **blocking-only** input. `SpecVerdict` is `pass | fail | unknown` plus findings and has no merge/approve/authorize field or method (T12).
A future optional `spec: SpecVerdict | None = None` argument to `run_shepherd.decide`, following the `ci` precedent: `fail` joins `address-review` blockers, `unknown` waits like `ci-unknown`, `pass` never short-circuits reviewer or CI gates, and `None` keeps behavior identical. This spike does not edit `run_shepherd.py`.

**Rejected explicitly:** any design in which `openspec verify` (or an equivalent) replaces Shepherd, CI required checks, review policy, human approval or MCTL authorization.

## Change classes

Planning shapes inside the one DevLoop lifecycle, carried as advisory `x-mctl-change-class`:
- `trivial`: `skip_specs: true`, `proposal.md` + `tasks.md`, no `specs/`.
- `normal`: one capability delta.
- `governed`: deltas plus mandatory `design.md` and an explicit risk section.

A class never changes `control.requires_human_approval`, approval provenance or authorization.

## Schema comparison

Candidate: `tests/fixtures/sdd/schemas/mctl-devloop/schema.yaml` (proposal -> specs -> design -> tasks; apply tracks `tasks.md`). Archive/validate read deltas only from `specs/**/spec.md`, so any schema must generate under `specs/`.

| Criterion | built-in `spec-driven` | custom `mctl-devloop` | adapter-only |
|---|---|---|---|
| Runtime dependency on OpenSpec CLI/Node | yes | yes | none |
| Relies on experimental schema commands (v1.14.2) | no | yes | no |
| Keeps MCTL headings/EARS | no | via templates | yes (authored format) |
| Changes investigator/publish guards | yes | yes | no |
| Reversible | partial | partial | yes |

## OpenSpec coupling inventory

Runtime coupling: **none**. No import, binary, network call or package. The only coupling is the documented file layout and the grammar, isolated in `spec_model.py` (verified against f80d860 in #659; deliberate deviations are the named `PROFILE` rules there). The import boundary is enforced by T13.

## Unverified facts

Resolved by #659, see `docs/spikes/659-openspec-grammar.md`: the spec and delta grammar, SHALL/MUST (a WARNING upstream, not an error), `proposal.md` sections (advisory warnings only at archive), near-miss and content-normalization rules, and `retire_capabilities` (a boolean upstream; refused by the MCTL profile). Still open:

- Real CLI v1.14.2 behavior: not run; every verdict comes from reading the source at f80d860.

## Migration cost and effect on state machine / APIs

- Adapter-only: none. No `.status.yaml` writer, `DevLoopWorkflow`, `decide` or API change; no proposal rewritten.
- If ever adopted: `TRIPLET` and `TRIPLET_FILENAMES`, both prompts, `_landed_triplet_defects`, context assembly, plus authoring a capability for each of the 93 existing mctl-agents proposals (or leaving them one-way).
- Gap found: approval records `approved_by`/`approved_at` but no content digest, so a format change cannot be bound to what was approved.

## Staged plan (only if `ADOPT_CONTRACT` is chosen later, after grammar verification)

1. Shadow-write a change package next to the triplet. 2. Implementer reads specs. 3. Investigator stops writing `requirements.md`. 4. Canonical-spec sync on merge.

## Follow-up issue drafts

1. **Record proposal content digest in approval.** Store a digest of the triplet in `approval` at approve time; refuse implementation if it differs.
2. **Verify OpenSpec grammar against f80d860.** Read the source at the pinned commit, port its rules with tests derived from upstream's (done in #659: `GRAMMAR_VERIFIED` removed); optionally add a dev-only CLI cross-check.
3. **Optional Shepherd `spec` input.** Add `spec: SpecVerdict | None = None` to `decide`, blocking-only.
4. **Capability catalogue.** Author canonical specs for the DevLoop approval gate and one more capability, to measure real authoring cost.
