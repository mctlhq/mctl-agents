"""The implementer must enforce control.requires_human_approval (gitops#986).

`accepted` alone is not authorisation. The flag was written by the
investigator, self-checked once at publish time, and then read by nothing:
a proposal committed as `accepted` was indistinguishable from one a human
approved.

These tests assert on the REFUSAL. A test asserting that a properly approved
proposal is accepted passes on the unenforced code too and proves nothing, so
the approved cases here are only guards against the gate being too strict.
"""
from pathlib import Path

import yaml

from orchestrator import run_implementer
from orchestrator.proposal_state import human_approval_satisfied


def write_proposal(state_dir: Path, service: str, slug: str, payload: dict) -> Path:
    d = state_dir / service / "proposals" / slug
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return d


REQUIRES = {"status": "accepted", "control": {"requires_human_approval": True}}


# --- the predicate, called directly rather than through the implementer -----

def test_requirement_without_an_approval_is_not_satisfied() -> None:
    assert human_approval_satisfied(REQUIRES) is False


def test_an_approval_naming_nobody_is_not_satisfied() -> None:
    # "unknown" is the literal default in mctl-api's registry and in
    # cwft-mctl-agents-approve.yaml, and a bare Temporal `approve` signal
    # lands the same value. Accepting it would leave the gate open through
    # exactly the paths that record nothing.
    for anonymous in ("unknown", "UNKNOWN", "  unknown  ", "", "none"):
        data = {**REQUIRES, "approval": {"approved_by": anonymous}}
        assert human_approval_satisfied(data) is False, anonymous


def test_a_named_approver_satisfies_the_requirement() -> None:
    data = {**REQUIRES, "approval": {"approved_by": "mashkovd"}}
    assert human_approval_satisfied(data) is True


def test_a_proposal_with_no_control_block_does_not_require_approval() -> None:
    # The incident-responder writes `status: accepted` with no control block
    # at all. Defaulting to deny would strand that entire path.
    assert human_approval_satisfied({"status": "accepted"}) is True
    assert human_approval_satisfied({"status": "accepted", "control": {}}) is True


def test_a_malformed_approval_block_does_not_satisfy() -> None:
    for approval in ("mashkovd", ["mashkovd"], {"approved_by": None}, {}):
        data = {**REQUIRES, "approval": approval}
        assert human_approval_satisfied(data) is False, approval


# --- the gate, through the implementer's own entry point -------------------

def test_scan_marks_an_unapproved_proposal(tmp_path: Path) -> None:
    write_proposal(tmp_path, "mctl-web", "issue-1", REQUIRES)
    refs = run_implementer.find_accepted_proposals(tmp_path)
    assert len(refs) == 1
    # Still returned: the proposal IS accepted, and the caller decides.
    assert refs[0].status == "accepted"
    assert refs[0].approval_ok is False


def test_scan_marks_an_approved_proposal(tmp_path: Path) -> None:
    write_proposal(tmp_path, "mctl-web", "issue-2",
                   {**REQUIRES, "approval": {"approved_by": "mashkovd"}})
    refs = run_implementer.find_accepted_proposals(tmp_path)
    assert refs[0].approval_ok is True


def test_implement_one_refuses_an_unapproved_proposal(tmp_path: Path) -> None:
    """The load-bearing assertion: the model must not run."""
    write_proposal(tmp_path, "mctl-web", "issue-3", REQUIRES)
    ref = run_implementer.find_accepted_proposals(tmp_path)[0]

    result = run_implementer.implement_one(ref, dry_run=False)

    assert result.pr_url is None
    assert result.skipped_reason is not None
    assert "requires_human_approval" in result.skipped_reason
    # A refusal is not a failure of the proposal's own quota.
    assert result.counts_toward_limit is False


def test_the_refusal_precedes_the_dry_run_shortcut(tmp_path: Path) -> None:
    # dry_run returns early, so if the gate sat after it a --dry-run would
    # report an unapproved proposal as runnable.
    write_proposal(tmp_path, "mctl-web", "issue-4", REQUIRES)
    ref = run_implementer.find_accepted_proposals(tmp_path)[0]
    result = run_implementer.implement_one(ref, dry_run=True)
    assert "requires_human_approval" in (result.skipped_reason or "")
