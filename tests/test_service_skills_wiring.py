"""Wiring between service-skill resolution and the runner drivers
(mctlhq/mctl-agents#305): everything between `resolve_bundle` and the prompt
the SDK receives, on both the investigator and implementer paths."""
from pathlib import Path

import pytest

from orchestrator import service_skills


def _investigator():
    """`run_issue_investigator` needs Python >= 3.11 (`datetime.UTC`); skip
    its wiring tests on older local interpreters, run them in CI."""
    return pytest.importorskip(
        "orchestrator.run_issue_investigator", reason="needs Python >= 3.11"
    )


def test_splice_lands_between_context_and_what_to_produce():
    investigator = _investigator()
    prompt = "intro\n## Your working context\nstuff\n## What to produce\ntail\n"
    spliced = investigator._splice_service_skills(prompt, "<service_skills>BLOCK</service_skills>")
    ctx = spliced.index("## Your working context")
    block = spliced.index("BLOCK")
    produce = spliced.index("## What to produce")
    assert ctx < block < produce
    assert spliced.count("BLOCK") == 1


def test_splice_raises_on_drifted_template():
    investigator = _investigator()
    with pytest.raises(RuntimeError, match="splice anchor"):
        investigator._splice_service_skills("no anchor here", "BLOCK")


def test_investigator_build_prompt_contains_block_and_anchor():
    """The real template still carries the anchor, and a non-empty block
    lands inside the rendered prompt exactly once."""
    investigator = _investigator()
    ref = investigator.IssueRef(
        owner="mctlhq", repo="x", number=1, url="https://github.com/mctlhq/x/issues/1"
    )
    issue = investigator.IssueData(ref=ref, title="t", body="b", state="OPEN")
    prompt = investigator._build_prompt(
        issue, "x", "slug", service_skills_block="<service_skills>WIRED</service_skills>"
    )
    assert prompt.count("WIRED") == 1
    assert prompt.index("WIRED") < prompt.index("## What to produce")


def test_prompt_block_legacy_mode_never_touches_the_clone(monkeypatch):
    """R23: `legacy` mode returns "" without reading the target repository —
    proven by handing it a path that does not exist."""
    investigator = _investigator()
    monkeypatch.delenv("ISSUE_INVESTIGATOR_RESOLVER_MODE", raising=False)
    assert investigator._service_skills_prompt_block(Path("/nonexistent/never-read")) == ""


def test_implementer_resolution_disabled_policy_skips_pin_sha(monkeypatch):
    """R17 ordering: the implementer manifest declares no spec.serviceSkills
    block today, so resolution must return an empty bundle WITHOUT invoking
    pin_sha (no git subprocess for a disabled policy)."""
    run_implementer = pytest.importorskip(
        "orchestrator.run_implementer", reason="needs Python >= 3.11"
    )

    def _boom(*a, **k):  # noqa: ANN002, ANN003
        raise AssertionError("pin_sha must not run for a disabled policy")

    monkeypatch.setattr(run_implementer, "service_skill_pin_sha", _boom)
    bundle = run_implementer._resolve_implementer_service_skills(Path("/nonexistent"), "feat/agents-x")
    assert bundle.skills == ()


def test_implementer_build_prompt_carries_block():
    run_implementer = pytest.importorskip(
        "orchestrator.run_implementer", reason="needs Python >= 3.11"
    )
    ref = run_implementer.ProposalRef(
        service="x", slug="s", proposal_dir=Path("/tmp/p"), status="accepted"
    )
    prompt = run_implementer._build_prompt(
        ref, service_skills_block="<service_skills>WIRED</service_skills>"
    )
    assert prompt.count("WIRED") == 1


def test_agent_authored_constant_is_what_wiring_depends_on():
    assert set(service_skills.AGENT_AUTHORED_AGENTS) == {"implementer", "shepherd"}
