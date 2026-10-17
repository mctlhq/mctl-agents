"""The subset of OpenSpec's validator that decides whether archive may proceed.

Upstream archive (src/core/archive.ts) validates every delta before the merge
and every rebuilt spec after it, both non-strict: ERRORs block, WARNINGs do
not. Only the rules behind those ERRORs, plus the SHALL/MUST WARNING, are
modelled here (``PROFILE["validator-subset"]``). VA = validation/validator.ts.
"""

from __future__ import annotations

from dataclasses import dataclass

from orchestrator.sdd.spec_model import (
    Delta,
    has_requirements_section,
    has_shall_or_must,
    parse_spec,
    purpose,
    structure_issues,
)


@dataclass(frozen=True)
class Issue:
    level: str  # ERROR | WARNING
    message: str


def errors(issues: list[Issue]) -> list[str]:
    return [i.message for i in issues if i.level == "ERROR"]


def validate_delta(cap: str, delta: Delta) -> list[Issue]:
    """VA:validateChangeDeltaSpecs for one delta file."""
    issues: list[Issue] = []
    if delta.is_empty:
        if delta.sections:
            issues.append(Issue("ERROR", f"{cap}: delta sections were found, but no requirement entries parsed"))
        else:
            issues.append(Issue("ERROR", f"{cap}: no delta sections found"))
    for name in delta.orphaned:
        issues.append(Issue("WARNING", f"{cap}: requirement {name!r} is outside every delta section and is ignored"))
    for label, group in (("ADDED", delta.added), ("MODIFIED", delta.modified)):
        for r in group:
            text = r.statement
            if not text:
                issues.append(Issue("ERROR", f"{cap}: {label} {r.name!r} is missing requirement text"))
            elif not has_shall_or_must(text):
                issues.append(Issue("WARNING", f"{cap}: {label} {r.name!r} text has no SHALL or MUST"))
            if r.scenario_count() < 1:
                issues.append(Issue("ERROR", f"{cap}: {label} {r.name!r} must include at least one scenario"))
    return issues


def validate_spec(cap: str, text: str) -> list[Issue]:
    """The archive-blocking checks on a main spec (MP:parseSpec, Zod, VA:applySpecRules)."""
    issues = [Issue("ERROR", f"{cap}: {m}") for m in structure_issues(text)]
    if purpose(text) is None:
        issues.append(Issue("ERROR", f"{cap}: spec must have a Purpose section"))
    if not has_requirements_section(text):
        issues.append(Issue("ERROR", f"{cap}: spec must have a Requirements section"))
    spec = parse_spec(text)
    if not spec.requirements:
        issues.append(Issue("ERROR", f"{cap}: spec must have at least one requirement"))
    for r in spec.requirements:
        body = r.statement
        if not body:
            issues.append(Issue("ERROR", f"{cap}: requirement {r.name!r} is missing requirement text"))
        elif not has_shall_or_must(body):
            issues.append(Issue("WARNING", f"{cap}: requirement {r.name!r} text has no SHALL or MUST"))
        if r.scenario_count() < 1:
            issues.append(Issue("ERROR", f"{cap}: requirement {r.name!r} must have at least one scenario"))
    return issues
