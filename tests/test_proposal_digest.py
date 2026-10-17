"""The approval binds the content that was approved (mctl-agents#658).

`cwft-mctl-agents-approve.yaml` records `approval.content_digest` at the
flip, in busybox shell; the implementer recomputes it in Python before it
starts. The vectors below were produced by that shell, run in the CWFT's own
image (alpine/git:2.43.0), so a change to either side's framing fails here.

As in test_run_implementer_approval.py, the load-bearing assertions are the
refusals; the unchanged-proposal case only guards against a gate that is
too strict.
"""
from pathlib import Path

import pytest
import yaml

from orchestrator import run_implementer
from orchestrator.proposal_state import (
    BLOCKED_APPROVAL_DIGEST_MISMATCH,
    BLOCKED_APPROVAL_DIGEST_MISSING,
    PROPOSAL_DIGEST_FILES,
    approval_digest_problem,
    proposal_content_digest,
)

TRIPLET = {
    "requirements.md": b"# Requirements\n\n- R1 \xc3\xbcn\xc3\xafcode\n",
    "design.md": b"",
    "tasks.md": b"- [ ] 1. do\n",
}
# The shell's answer for TRIPLET (an empty design.md is present, not absent).
TRIPLET_DIGEST = "sha256:1330ca7acd9c5878e4d0ec7ffc530502af358d7a77e82df76b8a502eb0e183c3"
# The shell's answer for no design.md and a requirements.md without a newline.
NO_DESIGN = {"requirements.md": b"no trailing newline", "tasks.md": b"x\n"}
NO_DESIGN_DIGEST = "sha256:89880e19174c855c2242644ae19398c9f4e74351ff6df171ddbfea11c6489e8f"
# The shell's answer for an mctl-docs proposal, whose proposed-content.md is
# the verbatim deliverable (claude P2 on #675).
WITH_CONTENT = {
    "requirements.md": b"r\n",
    "design.md": b"d\n",
    "tasks.md": b"- [ ] 1. t\n",
    "proposed-content.md": b"# Page\n\nverbatim \xe2\x80\x94 body\n",
}
WITH_CONTENT_DIGEST = "sha256:dd25e016dd1087d9f95488f3dcde3cab47a6e7c241255afa26a8734d9ad27da9"


def _write_triplet(proposal_dir: Path, files: dict[str, bytes]) -> None:
    proposal_dir.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (proposal_dir / name).write_bytes(content)


def _approved(digest: object = TRIPLET_DIGEST) -> dict:
    approval: dict = {"approved_by": "mashkovd", "approved_at": "2026-10-17T00:00:00Z"}
    if digest is not None:
        approval["content_digest"] = digest
    return {
        "status": "accepted",
        "control": {"requires_human_approval": True},
        "approval": approval,
    }


def _proposal(tmp_path: Path, status: dict, files: dict[str, bytes] = TRIPLET) -> run_implementer.ProposalRef:
    proposal_dir = tmp_path / "mctl-web" / "proposals" / "issue-658"
    _write_triplet(proposal_dir, files)
    (proposal_dir / ".status.yaml").write_text(yaml.safe_dump(status), encoding="utf-8")
    [ref] = run_implementer.find_accepted_proposals(tmp_path)
    return ref


# --- the digest itself -------------------------------------------------------

def test_the_digest_matches_the_cwft_shell(tmp_path: Path) -> None:
    _write_triplet(tmp_path / "a", TRIPLET)
    _write_triplet(tmp_path / "b", NO_DESIGN)
    _write_triplet(tmp_path / "c", WITH_CONTENT)
    assert proposal_content_digest(tmp_path / "a") == TRIPLET_DIGEST
    assert proposal_content_digest(tmp_path / "b") == NO_DESIGN_DIGEST
    assert proposal_content_digest(tmp_path / "c") == WITH_CONTENT_DIGEST


def test_a_missing_file_is_not_an_empty_one(tmp_path: Path) -> None:
    _write_triplet(tmp_path / "empty", {**NO_DESIGN, "design.md": b""})
    _write_triplet(tmp_path / "absent", NO_DESIGN)
    assert proposal_content_digest(tmp_path / "empty") != proposal_content_digest(tmp_path / "absent")


def test_content_cannot_shift_between_files(tmp_path: Path) -> None:
    # Without the length prefix, moving bytes from the end of one file to the
    # start of the next could keep the concatenation, and the digest, equal.
    _write_triplet(tmp_path / "a", {"requirements.md": b"ab", "design.md": b"c", "tasks.md": b""})
    _write_triplet(tmp_path / "b", {"requirements.md": b"a", "design.md": b"bc", "tasks.md": b""})
    assert proposal_content_digest(tmp_path / "a") != proposal_content_digest(tmp_path / "b")


def test_an_unreadable_file_raises_rather_than_hashing_as_absent(tmp_path: Path, monkeypatch) -> None:
    _write_triplet(tmp_path, TRIPLET)
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == "design.md":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    with pytest.raises(OSError):
        proposal_content_digest(tmp_path)


# --- the predicate -----------------------------------------------------------

def test_an_unchanged_proposal_has_no_problem(tmp_path: Path) -> None:
    _write_triplet(tmp_path, TRIPLET)
    assert approval_digest_problem(_approved(), tmp_path) is None


@pytest.mark.parametrize("name", PROPOSAL_DIGEST_FILES)
def test_a_one_byte_edit_to_any_file_is_drift(tmp_path: Path, name: str) -> None:
    _write_triplet(tmp_path, WITH_CONTENT)
    path = tmp_path / name
    path.write_bytes(path.read_bytes() + b" ")
    problem = approval_digest_problem(_approved(WITH_CONTENT_DIGEST), tmp_path)
    assert problem is not None
    code, recorded, current = problem
    assert code == BLOCKED_APPROVAL_DIGEST_MISMATCH
    assert recorded == WITH_CONTENT_DIGEST
    assert current == proposal_content_digest(tmp_path)


@pytest.mark.parametrize("name", PROPOSAL_DIGEST_FILES)
def test_deleting_or_adding_a_file_is_drift(tmp_path: Path, name: str) -> None:
    _write_triplet(tmp_path, WITH_CONTENT)
    (tmp_path / name).unlink()
    problem = approval_digest_problem(_approved(WITH_CONTENT_DIGEST), tmp_path)
    assert problem is not None and problem[0] == BLOCKED_APPROVAL_DIGEST_MISMATCH
    # ...and a file that appears after the flip is drift too.
    _write_triplet(tmp_path / "later", TRIPLET)
    (tmp_path / "later" / "proposed-content.md").write_bytes(b"added after approval\n")
    problem = approval_digest_problem(_approved(), tmp_path / "later")
    assert problem is not None and problem[0] == BLOCKED_APPROVAL_DIGEST_MISMATCH


@pytest.mark.parametrize("digest", [None, "", "   ", 42, ["sha256:x"], {"sha256": "x"}])
def test_a_missing_or_malformed_digest_never_counts_as_a_match(tmp_path: Path, digest: object) -> None:
    # Pre-rollout approvals are not grandfathered: an approval that records
    # no digest is refused, never read as "nothing to compare".
    _write_triplet(tmp_path, TRIPLET)
    problem = approval_digest_problem(_approved(digest), tmp_path)
    assert problem is not None and problem[0] == BLOCKED_APPROVAL_DIGEST_MISSING


def test_a_digest_of_other_content_is_drift(tmp_path: Path) -> None:
    _write_triplet(tmp_path, TRIPLET)
    problem = approval_digest_problem(_approved(NO_DESIGN_DIGEST), tmp_path)
    assert problem is not None and problem[0] == BLOCKED_APPROVAL_DIGEST_MISMATCH


def test_a_proposal_that_never_asked_for_approval_is_not_checked(tmp_path: Path) -> None:
    # incident-responder records: accepted, no control block, no approval.
    _write_triplet(tmp_path, TRIPLET)
    assert approval_digest_problem({"status": "accepted"}, tmp_path) is None
    assert approval_digest_problem(
        {"status": "accepted", "control": {"requires_human_approval": False}}, tmp_path
    ) is None


# --- the gate, through the implementer's own entry point ---------------------

@pytest.mark.parametrize(("files", "digest"), [(TRIPLET, TRIPLET_DIGEST), (WITH_CONTENT, WITH_CONTENT_DIGEST)])
def test_an_unchanged_approved_proposal_passes_the_gate(tmp_path: Path, files: dict, digest: str) -> None:
    ref = _proposal(tmp_path, _approved(digest), files)
    result = run_implementer.implement_one(ref, dry_run=True)
    assert result.blocked is None
    assert result.skipped_reason == "dry-run"


@pytest.mark.parametrize("name", PROPOSAL_DIGEST_FILES)
def test_the_implementer_refuses_drift_in_any_file(tmp_path: Path, name: str) -> None:
    ref = _proposal(tmp_path, _approved(WITH_CONTENT_DIGEST), WITH_CONTENT)
    path = ref.proposal_dir / name
    path.write_bytes(path.read_bytes() + b"!")

    result = run_implementer.implement_one(ref, dry_run=False)

    assert result.blocked == BLOCKED_APPROVAL_DIGEST_MISMATCH
    assert result.pr_url is None
    assert result.counts_toward_limit is False
    assert WITH_CONTENT_DIGEST in (result.skipped_reason or "")
    data = yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))
    assert data["status"] == "accepted"
    assert data["blocked"]["code"] == BLOCKED_APPROVAL_DIGEST_MISMATCH
    assert "content_digest" in data["blocked"]["remedy"]
    # The approval itself is never rewritten to match.
    assert data["approval"]["content_digest"] == WITH_CONTENT_DIGEST


def test_the_implementer_refuses_an_approval_without_a_digest(tmp_path: Path) -> None:
    ref = _proposal(tmp_path, _approved(None))
    result = run_implementer.implement_one(ref, dry_run=True)
    assert result.blocked == BLOCKED_APPROVAL_DIGEST_MISSING
    assert result.pr_url is None


def test_the_refusal_precedes_the_dry_run_shortcut_and_writes_nothing(tmp_path: Path) -> None:
    ref = _proposal(tmp_path, _approved())
    (ref.proposal_dir / "tasks.md").write_bytes(b"- [ ] 1. something else\n")
    before = ref.status_path.read_bytes()

    result = run_implementer.implement_one(ref, dry_run=True)

    assert result.blocked == BLOCKED_APPROVAL_DIGEST_MISMATCH
    assert ref.status_path.read_bytes() == before


def test_the_drift_marker_is_written_exactly_once(tmp_path: Path) -> None:
    ref = _proposal(tmp_path, _approved())
    (ref.proposal_dir / "requirements.md").write_bytes(b"rewritten\n")

    first = run_implementer.implement_one(ref, dry_run=False)
    first_bytes = ref.status_path.read_bytes()
    second = run_implementer.implement_one(ref, dry_run=False)

    assert first.blocked_is_new is True
    assert second.blocked_is_new is False
    assert ref.status_path.read_bytes() == first_bytes


def test_the_refusal_is_recorded_in_the_execution_evidence(tmp_path: Path) -> None:
    ref = _proposal(tmp_path, _approved())
    (ref.proposal_dir / "design.md").write_bytes(b"changed\n")
    result = run_implementer.implement_one(ref, dry_run=True)

    recorded: list[tuple[str, str]] = []

    class _Evidence:
        def set_outcome(self, code: str, reason: str) -> None:
            recorded.append((code, reason))

        def note_subject_pr_url(self, url: str) -> None:  # pragma: no cover - no PR here
            raise AssertionError(url)

        def note_proposal_status(self, *args: object) -> None:
            pass

    run_implementer._note_implement_evidence(_Evidence(), result, success_reason="pr-opened")  # type: ignore[arg-type]
    assert recorded == [("refused", "blocked-approval-digest-mismatch")]


def test_an_unreadable_triplet_is_this_proposals_error_not_a_batch_abort(tmp_path: Path, monkeypatch) -> None:
    # claude P2 on #675: an OSError raised out of implement_one escapes
    # _implement_refs' try/finally and stops every other proposal in the run.
    ref = _proposal(tmp_path, _approved())
    real = Path.read_bytes

    def failing(self: Path) -> bytes:
        if self.name == "tasks.md":
            raise PermissionError(13, "Permission denied", str(self))
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", failing)
    result = run_implementer.implement_one(ref, dry_run=False)

    assert result.error is not None and "approval digest could not be checked" in result.error
    assert result.blocked is None
    assert result.pr_url is None
    assert result.counts_toward_limit is False
    # A transient read failure leaves no durable marker behind.
    assert "blocked" not in yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("code", [BLOCKED_APPROVAL_DIGEST_MISMATCH, BLOCKED_APPROVAL_DIGEST_MISSING])
def test_the_refusal_names_every_file_the_digest_covers(code: str) -> None:
    # claude P2 on 450a6da: the message named three files after the digest
    # grew to four, misdescribing a proposed-content.md-only drift.
    message, remedy = run_implementer._approval_digest_message(code, TRIPLET_DIGEST, WITH_CONTENT_DIGEST)
    for name in PROPOSAL_DIGEST_FILES:
        assert name in remedy
        if code == BLOCKED_APPROVAL_DIGEST_MISMATCH:
            assert name in message
