"""One execution-authorization answer at every entry point (mctl-agents#703).

The implement sweep decides what to submit from `_parse_status_yaml`, which
reads `proposal_state.execution_authorization`. A run that reaches the
implementer any other way (a manual trigger, a direct workflow submit, the
DevLoop after an approve) ends in `implement_one`. Before #703 the two read
different predicates: the sweep quarantined an auto-accepted record as
unauthorized, while the implementer's own first gate treated a record with no
`control` block as approved and only refused it later, at the grant check,
for a different reason.

These tests put the same record in front of both and fail on any divergence:
the sweep authorizes exactly what the implementer does not refuse, and a
refusal carries the sweep's own reason. They run the implementer as a dry run,
so nothing past the authorization gates executes.
"""
from pathlib import Path

import pytest
import yaml

from orchestrator import run_implementer
from orchestrator.proposal_state import (
    AUTHORIZATION_HUMAN_APPROVAL,
    BLOCKED_EXECUTION_UNAUTHORIZED,
    UNAUTHORIZED_ANONYMOUS_APPROVER,
    UNAUTHORIZED_LEGACY_AUTO_ACCEPTED,
    UNAUTHORIZED_NO_APPROVAL_GRANT,
    approval_requirement_met,
    proposal_content_digest,
)
from orchestrator.temporal.activities.gitops_state import _parse_status_yaml

# Taken at import time, before conftest's autouse stub replaces it.
REAL_AUTHORIZATION = run_implementer._execution_authorization

SERVICE = "mctl-web"
GRANT = "aar_contract01"


@pytest.fixture(autouse=True)
def _real_authorization(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_implementer, "_execution_authorization", REAL_AUTHORIZATION)


def _write(state_dir: Path, slug: str, payload: dict, *, bind_digest: bool = False) -> Path:
    d = state_dir / SERVICE / "proposals" / slug
    d.mkdir(parents=True)
    (d / "requirements.md").write_text(f"# {slug}\n", encoding="utf-8")
    if bind_digest:
        payload = {**payload, "approval": {**payload["approval"], "content_digest": proposal_content_digest(d)}}
    (d / ".status.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return d


def _sweep(proposal_dir: Path) -> tuple[str | None, str | None]:
    parsed = _parse_status_yaml((proposal_dir / ".status.yaml").read_text(encoding="utf-8"))
    return parsed[6], parsed[7]


def _implementer(state_dir: Path) -> run_implementer.ImplementResult:
    [ref] = run_implementer.find_accepted_proposals(state_dir)
    return run_implementer.implement_one(ref, dry_run=True)


REQUIRES = {"requires_human_approval": True}
NAMED = {"approved_by": "mashkovd", "approved_at": "2026-10-21T00:00:00Z"}

# (slug, status payload, whether the approval digest is bound, sweep answer)
CASES = [
    # The incident responder's auto-accept: no control block, no approval.
    ("incident-auto-accept", {"status": "accepted"}, False, (None, UNAUTHORIZED_LEGACY_AUTO_ACCEPTED)),
    # A waived gate is not a grant of authority.
    (
        "waived-gate",
        {"status": "accepted", "control": {"requires_human_approval": False}},
        False,
        (None, UNAUTHORIZED_LEGACY_AUTO_ACCEPTED),
    ),
    # An approve path ran and recorded nobody.
    (
        "anonymous-approver",
        {"status": "accepted", "approval": {"approved_by": "unknown"}},
        False,
        (None, UNAUTHORIZED_ANONYMOUS_APPROVER),
    ),
    # A named approver with no mctl-api grant to verify it against, with and
    # without a control block.
    (
        "named-no-grant",
        {"status": "accepted", "control": REQUIRES, "approval": NAMED},
        True,
        (None, UNAUTHORIZED_NO_APPROVAL_GRANT),
    ),
    (
        "named-no-grant-no-control",
        {"status": "accepted", "approval": NAMED},
        False,
        (None, UNAUTHORIZED_NO_APPROVAL_GRANT),
    ),
    # A record that is not a mapping where the approval should be.
    (
        "corrupt-approval",
        {"status": "accepted", "approval": "mashkovd"},
        False,
        (None, UNAUTHORIZED_LEGACY_AUTO_ACCEPTED),
    ),
    # The one authorized shape: a named human approval that names its grant.
    (
        "human-approval",
        {"status": "accepted", "control": REQUIRES, "approval": {**NAMED, "approval_ref": GRANT}},
        True,
        (AUTHORIZATION_HUMAN_APPROVAL, None),
    ),
]


@pytest.mark.parametrize(("slug", "payload", "bind", "expected"), CASES, ids=[c[0] for c in CASES])
def test_sweep_and_implementer_give_the_same_answer(
    tmp_path: Path, slug: str, payload: dict, bind: bool, expected: tuple[str | None, str | None],
) -> None:
    proposal_dir = _write(tmp_path, slug, payload, bind_digest=bind)

    assert _sweep(proposal_dir) == expected
    result = _implementer(tmp_path)

    authorization, reason = expected
    if authorization is None:
        assert result.blocked == BLOCKED_EXECUTION_UNAUTHORIZED, result
        assert result.skipped_reason == f"execution not authorized: {reason}"
        assert result.pr_url is None
        assert result.counts_toward_limit is False
    else:
        # Past every authorization gate: the dry run's own shortcut answers.
        assert result.blocked is None, result
        assert result.skipped_reason == "dry-run"


def test_a_missing_control_block_is_not_reported_as_approved(tmp_path: Path) -> None:
    """The record that never asked for approval meets its own requirement
    (the write-time question) and is still not authorized to execute."""
    payload = {"status": "accepted"}
    proposal_dir = _write(tmp_path, "no-control", payload)

    assert approval_requirement_met(payload) is True
    assert _sweep(proposal_dir) == (None, UNAUTHORIZED_LEGACY_AUTO_ACCEPTED)
    result = _implementer(tmp_path)
    assert result.blocked == BLOCKED_EXECUTION_UNAUTHORIZED


def test_a_real_refusal_writes_the_durable_marker_once(tmp_path: Path) -> None:
    proposal_dir = _write(tmp_path, "incident-marker", {"status": "accepted"})
    [ref] = run_implementer.find_accepted_proposals(tmp_path)

    first = run_implementer.implement_one(ref, dry_run=False)
    second = run_implementer.implement_one(ref, dry_run=False)

    assert first.blocked == second.blocked == BLOCKED_EXECUTION_UNAUTHORIZED
    assert (first.blocked_is_new, second.blocked_is_new) == (True, False)
    marker = yaml.safe_load((proposal_dir / ".status.yaml").read_text(encoding="utf-8"))["blocked"]
    assert marker["code"] == BLOCKED_EXECUTION_UNAUTHORIZED
    assert UNAUTHORIZED_LEGACY_AUTO_ACCEPTED in marker["message"]
    assert "triage" in marker["remedy"].lower()


def test_an_unreadable_status_file_is_an_error_not_an_authorization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write(tmp_path, "unreadable", {"status": "accepted", "control": REQUIRES,
                                    "approval": {**NAMED, "approval_ref": GRANT}}, bind_digest=True)
    [ref] = run_implementer.find_accepted_proposals(tmp_path)

    def boom(_path: Path) -> dict:
        raise OSError("disk went away")

    monkeypatch.setattr(run_implementer, "_load_status", boom)
    result = run_implementer.implement_one(ref, dry_run=True)

    assert result.blocked is None
    assert (result.error or "").startswith("execution authorization could not be checked")
    assert result.skipped_reason != "dry-run"
