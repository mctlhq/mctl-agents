# mctl-agents#659: OpenSpec grammar verified against f80d860 (v1.14.2)

Upstream source: OpenSpec at `f80d860fc510ba4f51180c8ddde35b91b32de657` (tag v1.14.2), read only; nothing was executed.
The tables compare upstream with the grammar **before** #659: `SM`, `AP`, `MD` and `CV` line numbers refer to
`orchestrator/sdd/spec_model.py`, `apply.py`, `metadata.py` and `convert.py` at the #659 base commit.

## Outcome

`GRAMMAR_VERIFIED` is gone. `spec_model.py` and `apply.py` now port the upstream rules below, and
`orchestrator/sdd/validate.py` ports the validator ERRORs that block archive (plus the SHALL/MUST WARNING).
Every deliberate difference is a named entry in `spec_model.PROFILE`, each with its own test in
`tests/test_sdd_grammar.py`:

| Profile rule | MCTL behaviour | Upstream |
|---|---|---|
| `no-retirement` | `retire_capabilities: true` is refused; an emptied capability fails the rebuilt-spec check | deletes an emptied capability's spec under strict conditions (§9) |
| `validator-subset` | only archive-blocking ERRORs and the SHALL/MUST WARNING | also advisory warnings: Purpose length, text length, unapplied prose, proposal Why/What Changes (§6, §10) |
| `purpose-level` | Purpose read from `## Purpose` only | `findSection` matches any header level (§10) |
| `lf-output` | rebuilt specs are LF | keeps an existing CRLF file CRLF (§11) |
| `sync-mode` | `mode="sync"`: ADDED on an existing requirement acts as MODIFIED | agent workflow template only, not CLI code (§8) |

Not modelled, and harmless to omit because each is diagnostic only: INFO for skipped `###` headers, the
"foreign heading travels with a block" warning, and the unapplied-prose warning. Fixtures gained `## Purpose`
(required upstream) and the "identical MODIFIED" row became `modified-replaces-whole-block`: upstream
replaces a MODIFIED block whole, and only a trim decides "identical".

Against the pre-#659 grammar, 43 of the 52 cases in `tests/test_sdd_grammar.py` fail.

Upstream abbreviations (all paths under `src/`):
- `RB` = `core/parsers/requirement-blocks.ts`, the delta reader and the main-spec block reader used by archive
- `RT` = `core/parsers/requirement-text.ts`, which holds the body, scenario and SHALL helpers
- `MP` = `core/parsers/markdown-parser.ts`, the section parser used by `validate <spec>` and the rebuilt-spec validation
- `CP` = `core/parsers/change-parser.ts`
- `SS` = `core/parsers/spec-structure.ts`
- `CF` = `core/parsers/code-fence.ts`
- `VA` = `core/validation/validator.ts`
- `VC` = `core/validation/constants.ts`
- `BS` = `core/schemas/base.schema.ts`
- `CS` = `core/schemas/change.schema.ts`
- `SA` = `core/specs-apply.ts`
- `AR` = `core/archive.ts`
- `CMS` = `core/change-metadata/schema.ts`
- `CMU` = `utils/change-metadata.ts`

Upstream has two layers, and they must not be mixed up:
- **Parser and merge (archive, hard abort).** `SA:buildUpdatedSpec` throws. Archive also runs `VA:validateChangeDeltaSpecs` beforehand in non-strict mode, so its ERRORs block and its WARNINGs do not (`AR` 1549-1569). It then runs `VA:validateSpecContent` on every rebuilt spec before writing anything, again non-strict (`AR` 1876-1879).
- **Validator.** ERROR, WARNING or INFO. `--strict` turns WARNINGs into failures (`VA:createReport` 1015-1033).

MCTL has no validator layer at all. `verify.py` only dry-runs `apply_change`.

---

## 1. Requirement header syntax

| Aspect | Upstream rule | Citation @ f80d860 | spec_model.py today | Verdict |
|---|---|---|---|---|
| Regex (delta and main-spec block reader) | `/^###\s*Requirement:\s*(.+)\s*$/i`. Case-insensitive, **zero or more** spaces after `###` (so `###Requirement:` matches), exactly level 3. A `####` line does not match. | `RB` const `REQUIREMENT_HEADER_REGEX` L38. Tests: `test/core/parsers/requirement-blocks.test.ts` L18 (mixed case), L31 (no space). | `_REQ = ^### Requirement:\s*(.+?)\s*$` (SM L15). Case-sensitive, exactly one space after `###`. | **DIFFERENT** (MCTL-STRICTER: `### requirement:` and `###Requirement:` are silently not requirements in MCTL) |
| Name normalisation | `normalizeRequirementName`: strip a CommonMark closing run `[ \t]+#+[ \t]*$`, then `trim()`. `C#` keeps its `#`. | `RB:normalizeRequirementName` L17-23 | Lazy `(.+?)\s*$` trims trailing whitespace only. `Foo ###` keeps `###` in the name. | **DIFFERENT** |
| Main-spec section scope | Blocks are read **only inside `## Requirements`**: `/^##\s+Requirements\s*$/i`, case-insensitive, fence-aware. A block ends at the next `### Requirement:` **or at any `## ` header**. Content after the section (`after`) is kept verbatim. If there is no `## Requirements`, one is appended. | `RB:extractRequirementsSection` L43-121 (L47, L64-69, L103) | `parse_spec` (SM L94-96) splits on `_REQ` anywhere in the file. A block runs to the next requirement header, so a trailing `## Notes` section is absorbed into the last requirement and lost if that requirement is MODIFIED or REMOVED. | **DIFFERENT** |
| Structural guard | A `### Requirement:` outside `## Requirements`, or a delta header (`## ADDED…`) in a main spec, makes the spec "structurally invalid" and archive aborts. Duplicate names (exact, after normalisation) are also an ERROR. Note this guard's own regex is `###\s+` (at least one space), unlike `RB`. | `SS:findMainSpecStructureIssues` L4-96. Applied by `SA` L425-433 and `VA:applySpecRules` L813-820. | Only duplicates are checked, and with casefold (SM L61-69, AP L58-60). No outside-section or delta-header check. | **DIFFERENT** (MCTL is stricter on duplicates, looser on the other two) |
| Spec path (`validate <spec>`, rebuilt-spec validation) | `MP` treats **every** `###` child of `## Requirements` as a requirement and strips an optional `Requirement:` prefix. The change parser narrows this to `Requirement:\s*\S`. | `MP:parseRequirements` L153-173 (L166). `CP:parseRequirements` L102-109. | Not modelled | **NOT-UPSTREAM in MCTL** (gap) |
| Fences, BOM, CRLF | Fence mask: the marker kind must match and the closer must be at least as long and carry no trailing text. BOM is stripped and CRLF normalised. | `CF:buildCodeFenceMask` L16-62. `RB:normalizeLineEndings` L205-209. | `_FENCE` toggles on any line starting with ```` ``` ```` or `~~~`, of any kind or length, trailing text allowed (SM L20, L76-78). No BOM strip. | **DIFFERENT** (minor) |

## 2. Scenario header syntax

| Aspect | Upstream rule | Citation | spec_model.py today | Verdict |
|---|---|---|---|---|
| What counts as a scenario | **ANY non-fenced level-4 header** `/^####\s+/`, not only `#### Scenario:`. This is deliberate parity between the delta path and the spec path. **The reviewer's claim is confirmed.** | `RT` const `SCENARIO_HEADER` L23-31. `RB:parseScenarioBlocks` L717-751 (comment L719-726). `RB:scenarioHeaderAt` L682-684. Test: `test/core/validation.test.ts` L1573 "any #### header counts as a scenario on the delta path". | `_SCEN = ^#### Scenario:\s*(.+?)\s*$` (SM L16). A `#### Edge case` is invisible. | **DIFFERENT** (MCTL-LOOSER: the scenario-loss guard misses non-`Scenario:` level-4 children) |
| Scenario name | Strip `####`, strip the closing `#` run (`[ \t]+#+[ \t]*$`), strip an optional `Scenario:` prefix (case-insensitive), `trim()`. **Case-sensitive** comparison. | `RB:scenarioNameFromHeaderText` L704-715 | `normalize_name` casefolds and collapses whitespace (SM L27-29, L47) | **DIFFERENT** |
| Empty scenario | A `####` header with an empty (whitespace-only) body is **not** a scenario on either path. | `RT:hasScenarioBody` L43-45. `RT:countScenarios` L109-111. `MP:parseScenarios` L175-190. | Header alone counts | **DIFFERENT** |
| Scenario body extent | Runs to the next non-fenced header of level 1-4 (`/^#{1,4}\s/`). Prose or bullets after the last scenario, and a `#####`, belong to that scenario. | `RT:readScenarioBodies` L118-133 (`SCENARIO_BODY_END` L34) | n/a (raw text only) | n/a |
| Scenario-loss comparison | Multiplicity-aware: N copies against M copies leaves max(0, N-M) missing. | `RB:diffScenarioNames` L610-640 | `set(...) - set(...)` (AP L101), so duplicates are collapsed | **DIFFERENT** (MCTL-LOOSER) |
| WHEN/THEN bullets | **Free-form.** Only a non-empty body is required. `- **WHEN**`/`- **THEN**` is template guidance only (`VC` `GUIDE_SCENARIO_FORMAT` L66-67, and the hint in `VA:emptyScenarioHint` L1087-1090). | as cited | Not checked | **MATCH** (both free-form) |
| Content between header and first scenario | This is the requirement body: lines after the header up to the **first header of any level** (`/^#{1,6}\s/`). Blank lines, fenced lines and `**Key**:` metadata lines are skipped (metadata is kept only when it is the whole body). | `RT:extractRequirementBody` L70-90 | Not modelled | n/a |

## 3. Delta section headings, RENAMED and REMOVED syntax

| Aspect | Upstream rule | Citation | spec_model.py today | Verdict |
|---|---|---|---|---|
| Section split | Every non-fenced `/^(##)\s+(.+)$/` line starts a section. The title is `trim()`med. | `RB:splitTopLevelSections` L395-419 | Every non-fenced line starting with `"## "` (SM L117-129) | MATCH (approx.) |
| Title match | `title.toLowerCase() === 'added requirements'` (and the same for the other three): **case-insensitive**, exactly one space between the words, trailing whitespace trimmed, **no trailing text**. `## ADDED  Requirements` (two spaces) does not match. | `RB:getSectionsCaseInsensitive` L432-446. Callers L230-233. `DELTA_SECTION_TITLES` L288-292. | `_OP = ^## (ADDED|MODIFIED|REMOVED|RENAMED) Requirements\s*$` (SM L17). Case-sensitive, one space after `##`. | **DIFFERENT** (MCTL-STRICTER: `## Added Requirements` raises "unknown delta section") |
| Repeated section | **All copies are merged** in document order. RENAMED pairs never span copies. | `RB` L387-394, L421-446, L253-259. Test: `test/core/parsers/delta-section-collision.test.ts`. | `GrammarError("duplicate … section")` (SM L123-124) | **MCTL-STRICTER** (profile rule; defensible, but it rejects upstream-valid input) |
| Non-delta `## ` section (e.g. `## Purpose`, `## Notes`) | **Allowed.** `## Purpose` in a delta seeds the Purpose of a NEW capability (`SA:extractPurposeSection` L1320-1353, used at L365 and L405). Other sections produce a WARNING ("not carried into the main spec"). Requirements under them produce a WARNING (orphaned). | `RB:findUnappliedContent` L344-379. `RB:findOrphanedRequirements` L303-328. `VA` L249-270. `SA` L244-258. | `GrammarError("unknown delta section")` (SM L126-127). **This rejects upstream's own delta template, which contains `## Purpose`** (`schemas/spec-driven/templates/spec.md` L3). | **DIFFERENT** (important) |
| RENAMED syntax | `/^\s*[-*+]?\s*FROM:\s*`?###\s*Requirement:\s*(.+?)`?\s*$/` and the same for `TO:`. The bullet is optional and may be any of `- * +`. Backticks are optional. **`FROM:`/`TO:`/`Requirement:` are case-sensitive** here (no `/i`). | `RB:parseRenamedPairs` L544-578 (L561-562). Test: `test/core/parsers/delta-list-markers.test.ts` L72-117. | `_FROM`/`_TO`: `^\s*-?\s*FROM:\s*`?### Requirement:…` (SM L18-19). Only the `-` bullet, exactly one space after `###`. | **DIFFERENT** (MCTL-STRICTER: `* FROM:` is silently ignored, so the rename never happens) |
| Unpaired FROM/TO | Recorded in `unpairedRenames`. Validate: ERROR. Archive: throws. | `RB` L148-152, L563-576. `VA` L234-242. `SA` L230-238. | Raises `GrammarError` (SM L136-146) | **MATCH** (both refuse) |
| REMOVED format | Two forms: (a) a plain `### Requirement: X` header; its body (`**Reason**:` / `**Migration**:` by convention) is kept in `removedBlocks` but has **no required fields**; (b) a bullet `/^\s*[-*+]\s*`?###\s*Requirement:\s*(.+?)`?\s*$/`. **Names only.** No scenario or text is required. | `RB:parseRemovedNames` L499-521. `RB` L186-189. `VA` L391-400 ("names only"). Test: `test/core/parsers/requirement-blocks.test.ts` L120 (Reason/Migration kept). | Header form only, via `_split_blocks` (SM L148-149). The bullet form `- ### Requirement: X` **is silently dropped**: no entry, no error. | **MCTL-LOOSER** (silent no-op on an upstream-valid removal) |
| Empty or missing delta | A file with no delta sections: ERROR "No delta sections found". Sections with no entries: ERROR. Archive throws "Delta parsing found no operations". | `VA` L272-282, L499-512. `SA` L356-362. | Empty `Delta()`, applied as a no-op | **MCTL-LOOSER** |

## 4. SHALL/MUST

| Aspect | Upstream rule | Citation | spec_model.py today | Verdict |
|---|---|---|---|---|
| Predicate | `/\b(SHALL|MUST)\b/`: whole word, **uppercase only**. A lowercase `shall` does not count. | `RT:containsShallOrMust` L52-54. Test: `test/core/validation.test.ts` L1166, L1377. | none | **NOT-UPSTREAM in MCTL** (no check) |
| Where it must appear | In the **requirement body**: lines after the `###` header up to the first header, excluding fences, blanks and `**Key**:` metadata. Multi-line, and it may sit on a later line. A keyword **only in the header does not count** (a dedicated hint is shown). | `RT:extractRequirementBody` L70-90. `VA:extractRequirementText` L1039-1047. `VA:buildMissingShallOrMustMessage` L1063-1074. | none | — |
| Severity, delta ADDED/MODIFIED | Empty body: **ERROR**. Body present but no keyword: **WARNING** (fails only under `--strict`). Archive runs non-strict, so it **does not block archive**. | `VA:validateChangeDeltaSpecs` L299-318 (ADDED), L345-364 (MODIFIED) | none | — |
| Severity, main spec | The same: empty body is an ERROR, no keyword is a WARNING. | `VA:applySpecRules` L867-886 | none | — |
| Schema level | Deliberately **not** in Zod. | `BS` L15-20 | — | — |
| Overall | Parser: no check. Validator: guidance only (WARNING), hard only in strict mode. | — | MCTL enforces nothing | **MCTL-LOOSER** (a profile rule could make it an ERROR: MCTL-STRICTER) |

## 5. At least one scenario per requirement

| Aspect | Upstream rule | Citation | spec_model.py today | Verdict |
|---|---|---|---|---|
| Delta ADDED/MODIFIED | `countScenarios < 1`: **ERROR**, which blocks archive. Empty-bodied headers do not count. | `VA` L330-333, L365-368. `RT:countScenarios` L109-111. | not checked | **MCTL-LOOSER** |
| Main spec, and the rebuilt spec at archive | Zod `scenarios.min(1)` gives an **ERROR** (via `convertZodErrors`). `applySpecRules` adds a WARNING with format guidance. Archive validates every rebuilt spec and aborts before any write. | `BS` L23-24. `VA` L62-66, L852-858. `AR` L1876-1879. | not checked (no rebuilt-spec validation) | **MCTL-LOOSER** |
| REMOVED | No scenario required | `VA` L391 | n/a | MATCH |

## 6. proposal.md

| Aspect | Upstream rule | Citation | MCTL today | Verdict |
|---|---|---|---|---|
| Required sections | `## Why` and `## What Changes`. Matched by `findSection`: **any header level**, case-insensitive exact title, recursive. A missing section throws "Change must have a Why/What Changes section". | `CP:parseChangeWithDeltas` L29-40. `MP:findSection` L140-151. `MP:parseChange` L56-67. | `CV:triplet_to_change` writes `proposal.md` = the `requirements.md` head plus its non-AC sections (CV L94-107, L131). There is no guarantee of either heading. | **DIFFERENT** |
| Lengths | Why: **min 50, max 1000 characters**. What Changes: min 1. Deltas: min 1, **max 10** (all Zod, so ERROR level within `validateChange`). | `CS` L24-32. `VC` L6, L10, L12, L34-36, L44. | none | — |
| `## Capabilities` (`### New Capabilities` / `### Modified Capabilities`), `## Impact` | **Template only, never validated.** | `schemas/spec-driven/templates/proposal.md` L1-34. `schemas/spec-driven/schema.yaml` L13-18. | — | **NOT-UPSTREAM** (as rules) |
| Is it enforced? | `validateChange` is called **only** from archive, and only in human mode (`if (!json)`), as **non-blocking** "Proposal warnings". `openspec validate <change>` runs only `validateChangeDeltaSpecs`. | `AR` L1462-1490. `commands/validate.ts` L337. Repo-wide grep: no other caller. | — | So effectively **advisory upstream** |
| Delta description length | Under 10 characters: WARNING (advisory) | `VA:applyChangeRules` L891-916 | — | — |

## 7. Name matching and normalisation (MODIFIED / REMOVED / RENAMED / ADDED)

| Aspect | Upstream rule | Citation | MCTL today | Verdict |
|---|---|---|---|---|
| Primary lookup | **Exact after `normalizeRequirementName`** (strip the closing `#` run and trim). **Case-sensitive. Interior whitespace is significant.** | `RB` L17-23. `SA` L436-440, L449, L508, L536, L567. | `_find` exact on the raw captured name (AP L39-43). There is no closing-`#` strip. | **MATCH** except the closing `#` run (minor DIFFERENT) |
| Near-miss fold | `foldRequirementName` = normalise, `toLowerCase()`, collapse `\s+` to a single space. **Used only for typo detection.** | `RB:foldRequirementName` L25-35 | `normalize_name` = whitespace collapse + `casefold()` (SM L27-29). `casefold` is broader than `toLowerCase` (e.g. ß). | **MATCH** (approx.) |
| Where near-miss aborts upstream | **REMOVED**: exact miss with a fold hit aborts. An exact miss with no fold hit only warns "treated as already removed". A new spec skips the check. **ADDED**: an exact identical match is a no-op, exact with different content aborts, a fold hit aborts. **RENAMED**: (a) source missing, target present, and a fold hit of the source that is not the target: abort; (b) a target fold hit on a key **other than the source**: abort, so a **case-only rename is allowed**. **MODIFIED: no near-miss check.** An exact miss gives "not found". | `SA` L505-531 (REMOVED), L564-591 (ADDED), L451-484 (RENAMED), L533-540 (MODIFIED) | `_find` applies the near-miss abort on **every** lookup (AP L44-47), including RENAMED TO without the source exemption. So `RENAMED Foo -> foo` **aborts in MCTL** but is allowed upstream. MODIFIED near-miss aborts with a different message (it aborts either way). | **DIFFERENT** (MCTL-STRICTER on case-only renames) |
| Duplicate keys inside a section | Exact (`normalizeRequirementName`) Set: ADDED, MODIFIED, REMOVED, RENAMED FROM and RENAMED TO each throw. | `SA` L260-308. `VA` L284-417. | Casefold duplicates for ADDED/MODIFIED/REMOVED (AP L61-68). **No duplicate FROM/TO check for RENAMED.** | **DIFFERENT** (stricter on case, looser on RENAMED) |
| Content equality (ADDED/MODIFIED identical: no-op) | `normalizeBlockRaw` = CRLF to LF, then `trim()`. Interior whitespace and blank lines are **significant**. | `SA:normalizeBlockRaw` L1013-1015. Used at L558, L573. | `normalize_content` collapses whitespace on every line and drops blank lines (SM L32-33) | **MCTL-LOOSER** (treats more content as identical) |

## 8. Apply order and conflict rules

| Aspect | Upstream rule | Citation | MCTL today | Verdict |
|---|---|---|---|---|
| Order | **RENAMED, then REMOVED, then MODIFIED, then ADDED** | `SA:buildUpdatedSpec` comment L445. Loops L447-591. | Same (AP L78-118) | **MATCH** |
| Pre-validation (throws before any apply) | Unpaired renames. Duplicates per section (incl. FROM/TO). **MODIFIED∩REMOVED, MODIFIED∩ADDED, ADDED∩REMOVED.** RENAMED-FROM∩REMOVED (folded). **MODIFIED naming a RENAMED FROM** ("must reference the NEW header"). RENAMED-TO∩ADDED. No operations at all. | `SA` L226-362 | Only: casefold duplicates in ADDED/MODIFIED/REMOVED, FROM∩REMOVED (folded), TO∩ADDED (AP L61-75). **Missing:** MODIFIED∩ADDED (MCTL then no-ops ADDED as identical), ADDED∩REMOVED (MCTL removes, then re-adds, then succeeds), MODIFIED-on-FROM (MCTL fails later, as "not found"), duplicate FROM/TO, empty delta. | **MCTL-LOOSER** |
| New capability (target missing) | MODIFIED or RENAMED present: throw. REMOVED present: a single warning ("N REMOVED requirement(s) ignored"). The skeleton is `# <id> Specification\n\n## Purpose\n<delta Purpose or "TBD - created by archiving change <name>. Update Purpose after archive.">\n\n## Requirements\n`. | `SA` L386-423. `SA:buildSpecSkeleton` L1393-1399. `VC` L20-21. | MODIFIED/RENAMED abort: MATCH (AP L52-54). The skeleton is `# {cap} Specification\n\n## Requirements` with **no `## Purpose`** (AP L55). REMOVED warns per entry (AP L91). | **DIFFERENT** (the skeleton lacks Purpose, which the upstream rebuilt-spec validator rejects) |
| MODIFIED target missing | Throw "not found" | `SA` L538-540 | Abort (AP L97-98) | **MATCH** |
| MODIFIED drops scenarios | Throw. Any-`####`, multiplicity-aware. | `SA` L548-553 | Abort, but `Scenario:` headers only, set-based (AP L101-103) | **DIFFERENT** (see §2) |
| ADDED target exists | Identical (`normalizeBlockRaw`): no-op. Different: throw. Fold variant: throw. | `SA` L566-588 | Identical (`normalize_content`): no-op. Different: abort (archive) or modify (`mode="sync"`) (AP L109-118). | **MATCH** for archive. `sync` mirrors only the **agent workflow template** `core/templates/workflows/sync-specs.ts` L114 ("treat as implicit MODIFIED"), not CLI code. |
| RENAMED source missing, target present | No-op ("early-sync"), unless a near-miss of the source exists | `SA` L451-469 | No-op (AP L80-83), but with no source near-miss carve-out (`_find` aborts on any near-miss of either) | MATCH (approx.) |
| RENAMED target exists | Throw | `SA` L471-473 | Abort (AP L84-85) | **MATCH** |
| REMOVED target missing | WARNING, then no-op (unless near-miss) | `SA` L523-526 | Warn and skip (AP L90-91) | **MATCH** |
| Post-merge gate | Every rebuilt spec runs through `validateSpecContent`, **before any write**. Any ERROR aborts. That covers: missing Purpose or Requirements section, zero requirements, a requirement with no scenario, empty body, structural issues. | `AR` L1873-1960 | None | **MCTL-LOOSER** |
| Rebuild layout | `before` + `## Requirements` header + preamble + blocks (original order, renamed blocks kept in place, new ones appended) + `after`, joined with `\n\n`, blank runs collapsed outside fences, trailing `\n`. | `SA` L595-654 | preamble + blocks joined with `\n\n` (SM L99-102). No `after` slice. | **DIFFERENT** (see §1) |

## 9. Archive behaviour

| Aspect | Upstream rule | Citation | MCTL today | Verdict |
|---|---|---|---|---|
| Destination | `openspec/changes/archive/<YYYY-MM-DD>-<change>`, using the **local** date (`formatLocalDate`). **If the change name already starts with `\d{4}-\d{2}-\d{2}-`, it is kept as is (no double prefix).** An existing destination aborts **before any spec is touched**. | `AR` L53-57, L1653-1665, L1677. `core/root-selection.ts` L126. `utils/date.ts:formatLocalDate` L7-13. Tests: `test/core/archive.test.ts` L846, L878, L895. | `changes/archive/{date.today()}-{change}`, always prefixed (AP L155-156). An existing target aborts first (AP L159-160). | **DIFFERENT** (no date-prefix carve-out) |
| New capability spec created | Yes: skeleton plus ADDED. Created by `writeUpdatedSpec` with `mkdir -p`. | `SA` L404-423, L1239-1280 | Yes (AP L164-167) | **MATCH** (except the skeleton, §8) |
| Capability retirement | **EXISTS upstream.** It is a **boolean** `.openspec.yaml` marker, `retire_capabilities: true`. When a change's REMOVED entries take the **last** requirement of an **existing** capability, archive **deletes that main `spec.md`** (and prunes empty directories) instead of aborting. It is gated by all of: the marker is declared and valid, validation is not skipped, no requirement blocks are left, there is **no unaccounted content** (every non-blank line must be title, Purpose, header, statement or scenario bullets), the rebuilt spec's **only** validation ERROR is "Spec must have at least one requirement", and `counts.removed > 0`. Without the marker, archive aborts and names the marker. | `CMS` L22-31, **L54-61** (`retire_capabilities: z.boolean().optional()`). `CMU:readRetireCapabilitiesMarker` L335-346. `AR:isRetirableSpec` L72-80. `AR:decideSpecOutcome` L134-170. `AR` L1671, L1881-1960, L2049. `SA:retireSpec` L1029-1164. `SA:contentTheMergeCannotName` L775-973. Tests: `test/core/archive.test.ts` L4214 "capability retirement (#1302)", L4281, L8455. | `MD` accepts `retire_capabilities` but validates it as a **list of strings** (MD L98-100). Apply/archive do not model retirement: an emptied spec is written with zero requirements. | **DIFFERENT** (type mismatch: a list vs upstream's bool, so an upstream-valid `retire_capabilities: true` fails MCTL metadata validation and vice versa; the behaviour is also absent) |
| Change-spec discovery | `spec.md` at any depth, with id = the path segments. A root-level `specs/spec.md` is an ERROR. **Hidden (`.`) entries are skipped.** Other `.md` files carrying delta headers are an ERROR. | `utils/spec-discovery.ts:discoverSpecFiles` L39-77. `VA` L191-200, L490-497. | `rglob("spec.md")`, root-level aborts (AP L134-144). Does not skip dot-dirs or flag stray `.md` deltas. | MATCH (approx.) |

## 10. Main spec top-level structure

| Aspect | Upstream rule | Citation | MCTL today | Verdict |
|---|---|---|---|---|
| `# <title>` | Not required. Free text before `## Purpose`. | `MP:parseSpec` L29-54 | Kept in the preamble | MATCH |
| `## Purpose` | **Required, with a non-empty body** (any header level, case-insensitive): otherwise throws "Spec must have a Purpose section", which is an ERROR. Under 50 characters: WARNING. The placeholder (`TBD - created by archiving change …` or a TBD/TODO marker): WARNING. | `MP` L31-37. `VC` L7, L49-54. `VA` L822-841. `core/validation/purpose-placeholder.ts`. | Not required. **Every MCTL fixture spec lacks `## Purpose`**: `tests/fixtures/sdd/capabilities/devloop-approval/spec.md`, all `delta-rows/*/spec.md` and `expected.md`, and `changes/*/expected/spec.md`. Upstream would refuse to archive into any of them. | **DIFFERENT** (MCTL-LOOSER) |
| `## Requirements` | **Required** (throws otherwise). There must be ≥1 requirement (Zod `SPEC_NO_REQUIREMENTS`). Each requirement needs non-empty text and ≥1 scenario. Text over 500 characters: WARNING. | `MP` L39-41. `SS` L4-6. `VA` L843-859. `VC` L11, L55. | Not required | **MCTL-LOOSER** |
| Delta headers in a main spec | ERROR (structural) | `SS` L42-53 | Not checked | **MCTL-LOOSER** |

## 11. Other discrepancies and omissions

| Item | Upstream | Citation | MCTL | Verdict |
|---|---|---|---|---|
| Spec-path vs block-path requirement readers disagree upstream | `MP` takes any `###` under Requirements. `RB` takes only canonical headers and sweeps the rest into the preamble. Upstream documents this (it is why retirement asks the validator). | `AR` L60-71 comment | MCTL has one reader | Note for porting |
| Foreign headings inside a block | A `#`, `##` or `###` that is not a recognised boundary rides along in the block raw. Archive warns if MODIFIED or REMOVED drops it. | `SA:firstForeignTail` L675-701, L606-628 | Silently dropped | MCTL-LOOSER |
| MODIFIED header/key consistency | Re-checks that the MODIFIED raw header matches its key | `SA` L541-547 | n/a (name is derived from the header) | MATCH by construction |
| `skippedHeaders` INFO | A non-canonical `###` inside ADDED/MODIFIED gives an INFO note. It is absorbed into the preceding block's raw. | `RB` L123-132, L456-466. `VA` L219-229. | Absorbed silently | MATCH (behaviour), no diagnostic |
| Line endings on write | Preserves the existing CRLF/LF | `SA:writeUpdatedSpec` L1255-1265 | Writes LF | minor |
| Delta Purpose for an existing spec | Ignored with a warning | `SA` L377-385 | `GrammarError` (§3) | DIFFERENT |
| `normalize_name` used for duplicate detection in main specs | Upstream: exact. Fold variants are caught only by the ADDED/RENAMED near-miss checks. | `SS` L77-92 | casefold (SM L61-69) | MCTL-STRICTER (OK as a profile rule) |

---

## Upstream tests worth porting as minimal fixtures under `tests/fixtures/sdd/`

The only on-disk fixture upstream is `test/fixtures/tmp-init/openspec/{specs/alpha/spec.md, changes/c1/proposal.md, changes/c1/specs/alpha/spec.md}` (a minimal spec, proposal and delta triple; good as a smoke round-trip). Everything else is inline strings in the tests below.

- `test/core/parsers/requirement-blocks.test.ts` L18, L31, L58: a mixed-case `### requirement:` header and a `###Requirement:` header with no space must both parse.
- `test/core/parsers/requirement-blocks.test.ts` L47: a BOM before `## ADDED Requirements` must still parse.
- `test/core/parsers/requirement-blocks.test.ts` L120: a header-form REMOVED keeps its `**Reason**`/`**Migration**` body.
- `test/core/parsers/closed-requirement-heading.test.ts` L24-189: `### Requirement: Foo ###` equals `Foo` in every reader and in apply.
- `test/core/parsers/delta-list-markers.test.ts` L18-117: REMOVED bullets and RENAMED FROM/TO written with `-`, `*`, `+`, with or without backticks, indented or not.
- `test/core/parsers/delta-section-collision.test.ts` L18-133: repeated and case-variant section headers merge, and no FROM/TO pairs across copies.
- `test/core/parsers/renamed-pair-integrity.test.ts` L44-93: a TO before its FROM, a displaced FROM, a trailing FROM, and interleaved lines are all reported as unpaired.
- `test/core/parsers/orphaned-requirements.test.ts` L18-151: a requirement under `## Notes`, above the first section, or under a misspelled header gives a warning, not an error.
- `test/core/parsers/unapplied-content.test.ts` L28-110: prose outside the requirements produces warnings. `## Purpose` is exempt.
- `test/core/specs-apply.requirement-name-near-miss.test.ts` L94-161: the ADDED, REMOVED and RENAMED near-miss matrix, including a case-only rename allowed and a variant of a removed requirement allowed. **This directly contradicts MCTL's `_find`.**
- `test/core/validation.scenario-body-parity.test.ts` L89-201: an empty-bodied scenario does not count, and validate and archive agree.
- `test/core/validation.scenario-loss.test.ts` L77-229: MODIFIED drops a scenario; duplicate scenario names; a rename then a modify; chained renames.
- `test/core/validation.test.ts` L655, L765, L835, L1166, L1431, L1490, L1573: SHALL in the body vs the header, lowercase `shall` rejected, a metadata-only body, a stray `###` ending the body, any `####` as a scenario.
- `test/core/parsers/markdown-parser.test.ts` L80, L96, L202, L212: a missing Purpose, Requirements, Why or What Changes section throws.
- `test/core/archive.test.ts` L846-895: the archive date prefix and an existing `YYYY-MM-DD-` prefix kept. L1453: REMOVED allowed on a new spec (issue #403). L1506: delta Purpose carried into a new spec. L4214-4300: `retire_capabilities: true` retirement, plus the abort without the marker.

## Unverifiable here

- Real CLI behaviour: not run, by instruction. All verdicts above come from reading the source.
- Whether upstream `--strict` is used by any MCTL-relevant flow: n/a. Upstream archive itself always runs the validator non-strict (`AR` L1458 `new Validator()`, with no strict argument).
