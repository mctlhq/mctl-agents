"""OpenSpec v1.14.2 / f80d860 grammar, ported case by case (mctl-agents#659).

Run against the pre-#659 grammar, 43 of these 52 cases fail; the other nine
are controls for rules it already met (interleaved renames, metadata-only
bodies, two of the cross-section conflicts, the profile checks). Upstream
test files are cited by path under test/ at f80d860;
docs/spikes/659-openspec-grammar.md maps every rule to its source line.
"""

import datetime as dt
from pathlib import Path

import pytest

from orchestrator.sdd.apply import ApplyAbort, apply_change, archive_change, archive_target
from orchestrator.sdd.metadata import MetadataError, validate_mapping
from orchestrator.sdd.spec_model import PROFILE, GrammarError, parse_delta, parse_spec

PURPOSE = "## Purpose\nBilling rules for invoices, used by every grammar case in this file.\n\n"


def req(name: str, body: str = "The system SHALL bill.", scenarios: tuple[str, ...] = ("s1",)) -> str:
    out = f"### Requirement: {name}\n{body}\n"
    for s in scenarios:
        out += f"\n#### Scenario: {s}\n- **WHEN** a\n- **THEN** b\n"
    return out


def spec(*blocks: str, tail: str = "") -> str:
    return "# billing Specification\n\n" + PURPOSE + "## Requirements\n\n" + "\n".join(blocks) + tail


BASE = spec(req("Invoice Generation"), req("Late Fees"))


def names(text: str) -> list[str]:
    return [r.name for r in parse_spec(text).requirements]


def apply(delta: str, canonical: str | None = BASE, **kw):
    return apply_change({"billing": canonical} if canonical is not None else {}, {"billing": delta}, **kw)


# --- headers (test/core/parsers/requirement-blocks.test.ts L18, L31, L47) ------


@pytest.mark.parametrize(
    "header", ["### requirement: Credit Notes", "###Requirement: Credit Notes", "### REQUIREMENT: Credit Notes"]
)
def test_requirement_header_is_case_insensitive_and_needs_no_space(header):
    d = parse_delta(f"## ADDED Requirements\n\n{header}\nThe system SHALL credit.\n\n#### Scenario: s\n- x\n")
    assert [r.name for r in d.added] == ["Credit Notes"]


def test_a_bom_and_crlf_do_not_hide_the_first_section():
    d = parse_delta("﻿## ADDED Requirements\r\n\r\n" + req("Credit Notes").replace("\n", "\r\n"))
    assert [r.name for r in d.added] == ["Credit Notes"]


# test/core/parsers/closed-requirement-heading.test.ts
def test_a_closed_heading_names_the_same_requirement():
    res = apply("## MODIFIED Requirements\n\n" + req("Late Fees ###", "The system SHALL charge more."))
    assert "charge more" in res.specs["billing"]
    assert names(res.specs["billing"]) == ["Invoice Generation", "Late Fees"]
    assert parse_delta("## ADDED Requirements\n\n" + req("C#")).added[0].name == "C#"


# --- delta sections (test/core/parsers/delta-section-collision.test.ts) --------


def test_repeated_and_case_variant_sections_merge():
    d = parse_delta("## ADDED Requirements\n\n" + req("A") + "\n## Added Requirements\n\n" + req("B"))
    assert [r.name for r in d.added] == ["A", "B"]


def test_rename_pairs_never_span_section_copies():
    with pytest.raises(GrammarError):
        parse_delta(
            "## RENAMED Requirements\n- FROM: `### Requirement: A`\n\n"
            "## RENAMED Requirements\n- TO: `### Requirement: B`\n"
        )


# test/core/parsers/renamed-pair-integrity.test.ts
def test_interleaved_renames_are_refused_not_guessed():
    with pytest.raises(GrammarError):
        parse_delta(
            "## RENAMED Requirements\n"
            "- FROM: `### Requirement: a`\n- FROM: `### Requirement: b`\n"
            "- TO: `### Requirement: x`\n- TO: `### Requirement: y`\n"
        )


# test/core/parsers/delta-list-markers.test.ts
@pytest.mark.parametrize(
    "line", ["- ### Requirement: Late Fees", "* `### Requirement: Late Fees`", "  + ### Requirement: Late Fees"]
)
def test_removed_accepts_every_bullet_form(line):
    res = apply(f"## REMOVED Requirements\n\n{line}\n")
    assert names(res.specs["billing"]) == ["Invoice Generation"]


@pytest.mark.parametrize("marker", ["*", "+", ""])
def test_renamed_accepts_every_bullet_marker(marker):
    res = apply(
        f"## RENAMED Requirements\n{marker} FROM: `### Requirement: Late Fees`\n"
        f"{marker} TO: `### Requirement: Overdue Fees`\n"
    )
    assert names(res.specs["billing"]) == ["Invoice Generation", "Overdue Fees"]


# test/core/parsers/orphaned-requirements.test.ts, unapplied-content.test.ts
def test_a_non_delta_section_is_allowed_and_its_requirements_only_warn():
    res = apply("## Notes\n\n" + req("Stray") + "\n## ADDED Requirements\n\n" + req("Credit Notes"))
    assert names(res.specs["billing"]) == ["Invoice Generation", "Late Fees", "Credit Notes"]
    assert any("'Stray'" in w and "outside every delta section" in w for w in res.warnings)


# --- Purpose (test/core/archive.test.ts L1506; markdown-parser.test.ts L80) ----


def test_a_delta_purpose_seeds_a_new_capability():
    res = apply(PURPOSE + "## ADDED Requirements\n\n" + req("A"), canonical=None)
    assert res.specs["billing"].startswith("# billing Specification\n\n" + PURPOSE + "## Requirements\n")


def test_a_new_capability_without_a_purpose_gets_the_placeholder():
    res = apply("## ADDED Requirements\n\n" + req("A"), canonical=None, change="add-billing")
    assert (
        "## Purpose\nTBD - created by archiving change add-billing. Update Purpose after archive.\n"
        in res.specs["billing"]
    )


def test_a_commented_out_purpose_is_absent():
    res = apply("## Purpose\n<!-- one or two sentences -->\n\n## ADDED Requirements\n\n" + req("A"), canonical=None)
    assert "TBD - created by archiving change" in res.specs["billing"]


def test_a_delta_purpose_for_an_existing_spec_is_ignored_with_a_warning():
    res = apply("## Purpose\nSomething else entirely, long enough to count.\n\n## ADDED Requirements\n\n" + req("A"))
    assert PURPOSE in res.specs["billing"]
    assert any("delta Purpose ignored" in w for w in res.warnings)


def test_a_main_spec_without_a_purpose_cannot_be_archived_into():
    with pytest.raises(ApplyAbort, match="Purpose"):
        apply("## ADDED Requirements\n\n" + req("A"), canonical=BASE.replace(PURPOSE, ""))


# --- main-spec structure (src/core/parsers/spec-structure.ts) -----------------


def test_sections_after_requirements_survive_a_change_to_the_last_requirement():
    canonical = spec(req("Invoice Generation"), req("Late Fees"), tail="\n## Notes\n\nKeep me.\n")
    res = apply("## MODIFIED Requirements\n\n" + req("Late Fees", "The system SHALL charge more."), canonical)
    assert res.specs["billing"].endswith("## Notes\n\nKeep me.\n")
    res = apply("## REMOVED Requirements\n\n- `### Requirement: Late Fees`\n", canonical)
    assert res.specs["billing"].endswith("## Notes\n\nKeep me.\n")


@pytest.mark.parametrize(
    "bad",
    [
        "# billing Specification\n\n"
        + PURPOSE
        + "### Requirement: Outside\nThe system SHALL x.\n\n#### Scenario: s\n- x\n\n## Requirements\n\n"
        + req("A"),
        spec(req("A")) + "\n## ADDED Requirements\n\n" + req("B"),
    ],
)
def test_a_structurally_invalid_main_spec_is_refused(bad):
    with pytest.raises(ApplyAbort, match="structurally invalid"):
        apply("## ADDED Requirements\n\n" + req("C"), canonical=bad)


def test_a_fenced_requirement_header_is_not_a_requirement():
    canonical = spec(req("A", "The system SHALL x.\n\n```md\n### Requirement: Fenced\n```"))
    assert names(canonical) == ["A"]
    # ``` with an info string opens; ~~~ does not close a backtick fence.
    canonical = spec(req("A", "The system SHALL x.\n\n```python\n~~~\n### Requirement: Fenced\n```"))
    assert names(canonical) == ["A"]


# --- scenarios (test/core/validation.scenario-loss.test.ts, ...-body-parity) ---


def test_any_level_four_header_is_a_scenario_the_loss_guard_protects():
    canonical = spec(
        req("Late Fees", scenarios=("s1", "Edge case")).replace("#### Scenario: Edge case", "#### Edge case")
    )
    with pytest.raises(ApplyAbort, match="drops scenarios"):
        apply("## MODIFIED Requirements\n\n" + req("Late Fees", "The system SHALL charge."), canonical)


def test_scenario_names_are_case_sensitive():
    canonical = spec(req("Late Fees", scenarios=("S1",)))
    with pytest.raises(ApplyAbort, match="drops scenarios"):
        apply("## MODIFIED Requirements\n\n" + req("Late Fees", scenarios=("s1",)), canonical)


def test_scenario_loss_counts_duplicates():
    canonical = spec(req("Late Fees", scenarios=("s1", "s1")))
    with pytest.raises(ApplyAbort, match="drops scenarios"):
        apply("## MODIFIED Requirements\n\n" + req("Late Fees", scenarios=("s1",)), canonical)


def test_an_empty_scenario_is_no_scenario():
    with pytest.raises(ApplyAbort, match="at least one scenario"):
        apply("## ADDED Requirements\n\n### Requirement: A\nThe system SHALL x.\n\n#### Scenario: empty\n\n")


# --- requirement text and SHALL/MUST (test/core/validation.test.ts) ----------


def test_an_empty_requirement_body_is_an_error():
    with pytest.raises(ApplyAbort, match="missing requirement text"):
        apply("## ADDED Requirements\n\n### Requirement: The system SHALL x\n\n#### Scenario: s\n- x\n")


@pytest.mark.parametrize("body", ["The system shall bill.", "**Note**: billing happens.\nIt bills."])
def test_a_missing_shall_or_must_only_warns(body):
    res = apply("## ADDED Requirements\n\n" + req("Credit Notes", body))
    assert any("no SHALL or MUST" in w for w in res.warnings)


def test_a_metadata_only_body_is_the_body():
    res = apply("## ADDED Requirements\n\n" + req("Credit Notes", "**Constraint**: The system MUST credit."))
    assert not any("no SHALL or MUST" in w for w in res.warnings)


# --- names and conflicts (test/core/specs-apply.requirement-name-near-miss.test.ts)


def test_a_case_only_rename_is_allowed():
    res = apply("## RENAMED Requirements\n- FROM: `### Requirement: Late Fees`\n- TO: `### Requirement: late fees`\n")
    assert names(res.specs["billing"]) == ["Invoice Generation", "late fees"]


def test_a_case_variant_of_a_requirement_the_delta_removes_may_be_added():
    res = apply(
        "## REMOVED Requirements\n\n- `### Requirement: Late Fees`\n\n## ADDED Requirements\n\n" + req("late fees")
    )
    assert names(res.specs["billing"]) == ["Invoice Generation", "late fees"]


def test_two_added_requirements_differing_only_in_case_are_refused():
    with pytest.raises(ApplyAbort, match="only in case"):
        apply("## ADDED Requirements\n\n" + req("Credit Notes") + "\n" + req("credit notes"))


def test_identical_means_identical_after_trimming_only():
    with pytest.raises(ApplyAbort, match="different content"):
        apply("## ADDED Requirements\n\n" + req("Late Fees").replace("SHALL bill", "SHALL  bill"))


@pytest.mark.parametrize(
    "delta",
    [
        "## MODIFIED Requirements\n\n" + req("Late Fees") + "\n## ADDED Requirements\n\n" + req("Late Fees"),
        "## ADDED Requirements\n\n"
        + req("Late Fees")
        + "\n## REMOVED Requirements\n\n- `### Requirement: Late Fees`\n",
        "## RENAMED Requirements\n- FROM: `### Requirement: Late Fees`\n- TO: `### Requirement: Fees`\n\n"
        "## MODIFIED Requirements\n\n" + req("Late Fees"),
        "## RENAMED Requirements\n- FROM: `### Requirement: Late Fees`\n- TO: `### Requirement: X`\n"
        "- FROM: `### Requirement: Late Fees`\n- TO: `### Requirement: Y`\n",
    ],
)
def test_cross_section_conflicts_are_refused_before_any_change(delta):
    with pytest.raises(ApplyAbort):
        apply(delta)


@pytest.mark.parametrize("delta", ["", "Just prose.\n", "## ADDED Requirements\n\nNo requirement here.\n"])
def test_a_delta_with_no_operations_is_refused(delta):
    with pytest.raises(ApplyAbort):
        apply(delta)


# --- archive (test/core/archive.test.ts L846-895, L4214-4300) ------------------


def test_a_dated_change_name_is_not_prefixed_twice():
    day = dt.date(2026, 10, 17)
    assert archive_target("add-billing", day) == "2026-10-17-add-billing"
    assert archive_target("2026-10-01-add-billing", day) == "2026-10-01-add-billing"


def test_retire_capabilities_is_a_boolean_upstream():
    validate_mapping({"schema": "mctl-devloop", "retire_capabilities": True})
    with pytest.raises(MetadataError):
        validate_mapping({"schema": "mctl-devloop", "retire_capabilities": ["billing"]})


def _change(tmp_path: Path, delta: str, meta: str | None = None) -> Path:
    root = tmp_path / "openspec"
    (root / "specs" / "billing").mkdir(parents=True)
    (root / "specs" / "billing" / "spec.md").write_text(spec(req("Late Fees")))
    change = root / "changes" / "drop-fees"
    (change / "specs" / "billing").mkdir(parents=True)
    (change / "specs" / "billing" / "spec.md").write_text(delta)
    if meta is not None:
        (change / ".openspec.yaml").write_text(meta)
    return root


@pytest.mark.parametrize("meta", [None, "schema: mctl-devloop\nretire_capabilities: true\n"])
def test_profile_no_retirement_an_emptied_capability_is_refused(tmp_path, meta):
    root = _change(tmp_path, "## REMOVED Requirements\n\n- `### Requirement: Late Fees`\n", meta)
    before = (root / "specs/billing/spec.md").read_bytes()
    with pytest.raises(ApplyAbort):
        archive_change(root, "drop-fees", today=dt.date(2026, 10, 17))
    assert (root / "specs/billing/spec.md").read_bytes() == before
    assert (root / "changes/drop-fees").is_dir()


def test_hidden_directories_are_not_capabilities(tmp_path):
    root = _change(tmp_path, "## ADDED Requirements\n\n" + req("Credit Notes"))
    hidden = root / "changes/drop-fees/specs/.draft"
    hidden.mkdir()
    (hidden / "spec.md").write_text("## MODIFIED Requirements\n\n" + req("Nope"))
    archive_change(root, "drop-fees", today=dt.date(2026, 10, 17))
    assert not (root / "specs/.draft").exists()


# --- the remaining named profile rules ----------------------------------------


def test_profile_validator_subset_advisory_warnings_are_not_modelled():
    # Upstream warns (strict-only) on a Purpose under 50 characters.
    res = apply("## ADDED Requirements\n\n" + req("A"), canonical=BASE.replace(PURPOSE, "## Purpose\nShort.\n\n"))
    assert res.warnings == []


def test_profile_purpose_level_only_a_level_two_purpose_counts():
    with pytest.raises(ApplyAbort, match="Purpose"):
        apply("## ADDED Requirements\n\n" + req("A"), canonical=BASE.replace("## Purpose", "### Purpose"))


def test_profile_lf_output_a_crlf_spec_is_rebuilt_with_lf():
    res = apply("## ADDED Requirements\n\n" + req("A"), canonical=BASE.replace("\n", "\r\n"))
    assert "\r" not in res.specs["billing"]


def test_every_profile_rule_has_a_test():
    # sync-mode is exercised by tests/fixtures/sdd/delta-rows/sync-divergence-sync-mode.
    tested = {"no-retirement", "validator-subset", "purpose-level", "lf-output", "sync-mode"}
    assert set(PROFILE) == tested
