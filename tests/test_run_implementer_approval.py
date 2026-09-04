"""The implementer must enforce control.requires_human_approval (gitops#986).

`accepted` alone is not authorisation. The flag was written by the
investigator, self-checked once at publish time, and then read by nothing:
a proposal committed as `accepted` was indistinguishable from one a human
approved.

These tests assert on the REFUSAL. A test asserting that a properly approved
proposal is accepted passes on the unenforced code too and proves nothing, so
the approved cases here are only guards against the gate being too strict.
"""
import builtins
import io
import os
import stat
from pathlib import Path
from unittest import mock

import yaml

from orchestrator import run_implementer
from orchestrator.proposal_state import (
    human_approval_satisfied,
    load_status,
    update_status_file,
)


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


def test_a_string_true_still_requires_approval() -> None:
    # A hand-edited or re-serialised status file can carry
    # `requires_human_approval: "true"`, which YAML leaves as a string. An
    # `is not True` identity check treats that as "not required" and skips the
    # gate — failing OPEN on exactly the value that asked for it (agy P1).
    # Every file in gitops today serialises a real boolean; this is about the
    # ones that will not.
    for truthy in ("true", "True", "TRUE", " yes ", "1"):
        data = {"status": "accepted", "control": {"requires_human_approval": truthy}}
        assert human_approval_satisfied(data) is False, truthy


def test_a_falsey_string_does_not_require_approval() -> None:
    for falsey in (False, "false", "no", "0", None):
        data = {"status": "accepted", "control": {"requires_human_approval": falsey}}
        assert human_approval_satisfied(data) is True, falsey


def test_an_unrecognised_requirement_value_fails_closed() -> None:
    # An allowlist of truthy spellings reads everything unlisted as falsey, so
    # a list, a dict or a typo would waive the gate (agy P1, second pass). The
    # test is against the falsey spellings instead, so the default is to
    # require approval.
    for weird in (["true"], {"v": True}, "required", "yep", 42, object()):
        data = {"status": "accepted", "control": {"requires_human_approval": weird}}
        assert human_approval_satisfied(data) is False, weird


def test_a_malformed_control_block_fails_closed() -> None:
    # Absent means "never asked for approval". A control block that is present
    # but not a mapping asked for something unreadable, which is not the same
    # thing and must not be read as consent (agy P2).
    for malformed in ("requires_human_approval: true", ["requires_human_approval"], 42):
        assert human_approval_satisfied({"control": malformed}) is False, malformed


def test_a_hand_built_ref_is_not_assumed_approved() -> None:
    # approval_ok is fail-closed: a ref built without consulting the status
    # file has not established that anyone approved it, and defaulting to True
    # would make "forgot to pass it" indistinguishable from "a human approved
    # it" (agy P2).
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="issue-x", proposal_dir=Path("/nonexistent"),
        status="accepted",
    )
    assert ref.approval_ok is False
    result = run_implementer.implement_one(ref, dry_run=True)
    assert "requires_human_approval" in (result.skipped_reason or "")


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


# --- the approve command the investigator posts to real issues -------------

def test_the_posted_approve_command_is_valid(monkeypatch) -> None:
    """The comment tells a human exactly what to run, on a real GitHub issue.

    Requiring --approver on the CLI silently invalidated that copy-pasteable
    command (claude P2). Asserting the text against the CLI's own parser keeps
    the two from drifting again, rather than fixing the one line and leaving
    the next change to break it.
    """
    import re
    import shlex

    from orchestrator import run_issue_investigator
    from orchestrator.temporal import cli as temporal_cli

    posted: dict[str, str] = {}

    def fake_run(argv, **kwargs):
        posted["body"] = argv[argv.index("--body") + 1]
        return None

    monkeypatch.setattr(run_issue_investigator, "_run", fake_run)
    run_issue_investigator.post_proposal_comment(
        "https://github.com/mctlhq/mctl-web/issues/1", "mctl-web", "issue-1-x")

    m = re.search(r"`python -m orchestrator\.temporal\.cli (approve [^`]+)`",
                  posted["body"])
    assert m, f"no approve command found in the posted comment:\n{posted['body']}"

    argv = shlex.split(m.group(1).replace("<your-identity>", "someone"))
    parser = temporal_cli.build_parser()
    args = parser.parse_args(argv)          # raises SystemExit if it drifted
    assert args.command == "approve"
    assert args.approver == "someone"


# --- the write that could erase the requirement ----------------------------
# These belong with the gate rather than in a general status-file test:
# what makes the truncation window dangerous is precisely that an empty
# .status.yaml has no `control` block, and no control block waives approval.

def test_a_failed_dump_leaves_the_previous_status_intact(tmp_path: Path) -> None:
    path = tmp_path / ".status.yaml"
    path.write_text(yaml.safe_dump(REQUIRES), encoding="utf-8")

    class Unserialisable:
        pass

    try:
        update_status_file(path, "accepted", note=Unserialisable())
    except yaml.YAMLError:
        pass
    else:
        raise AssertionError("expected safe_dump to refuse an arbitrary object")

    # The file the implementer will read next must still carry the gate.
    survived = load_status(path)
    assert survived["control"] == {"requires_human_approval": True}
    assert human_approval_satisfied(survived) is False


def test_the_target_is_never_opened_for_writing(tmp_path: Path) -> None:
    # The behavioural statement, not an implementation detail: no writer may
    # open the real path in a mode that truncates it, because that is the
    # window in which a kill leaves an empty file — which reads as "approval
    # was never required".
    path = tmp_path / ".status.yaml"
    path.write_text(yaml.safe_dump(REQUIRES), encoding="utf-8")

    target = os.fspath(path)
    offenders: list[str] = []

    real_builtin_open = builtins.open
    real_os_open = os.open

    def spy_builtin(file, mode="r", *args, **kwargs):
        # `file` may be an integer descriptor — os.fdopen goes through
        # io.open too — and fspath() raises on those.
        if not isinstance(file, int) and os.fspath(file) == target:
            if any(c in mode for c in "wa+"):
                offenders.append(f"open({mode!r})")
        return real_builtin_open(file, mode, *args, **kwargs)

    def spy_os(target_path, flags, *args, **kwargs):
        if os.fspath(target_path) == target and flags & (
            os.O_TRUNC | os.O_WRONLY | os.O_RDWR
        ):
            offenders.append(f"os.open(flags={flags})")
        return real_os_open(target_path, flags, *args, **kwargs)

    # io.open as well as builtins.open: they are the same function object,
    # but Path.open resolves `io.open` at call time, so patching only
    # builtins leaves the path this test exists to forbid unobserved.
    with mock.patch.object(builtins, "open", spy_builtin), mock.patch.object(
        io, "open", spy_builtin
    ), mock.patch.object(os, "open", spy_os):
        update_status_file(path, "accepted")

    assert offenders == []
    assert load_status(path)["status"] == "accepted"


def test_a_rewrite_keeps_the_file_readable_by_other_components(tmp_path: Path) -> None:
    # The approve CWFT, the reconcile sweep and the implementer do not all
    # run as this user. A temp-file rename must not carry 0600 onto the
    # published file.
    path = tmp_path / ".status.yaml"
    path.write_text(yaml.safe_dump(REQUIRES), encoding="utf-8")
    # NOT 0o644: that is exactly what O_CREAT with 0o666 produces under the
    # umask 022 of a normal CI runner, so the assertion would hold whether
    # or not the mode was actually carried across the rename (claude P3).
    os.chmod(path, 0o640)

    update_status_file(path, "accepted")

    assert stat.S_IMODE(path.stat().st_mode) == 0o640


def test_no_temp_file_is_left_behind(tmp_path: Path) -> None:
    path = tmp_path / ".status.yaml"
    path.write_text(yaml.safe_dump(REQUIRES), encoding="utf-8")
    update_status_file(path, "accepted")
    assert [p.name for p in tmp_path.iterdir()] == [".status.yaml"]


def test_one_unparseable_status_does_not_stop_the_scan(tmp_path: Path) -> None:
    # `status in accepted_states` raises on an unhashable value, and it sits
    # outside the try that exists to skip one bad file — so a single
    # hand-edited status took down the whole scan and nothing ran.
    state = tmp_path / "agents-state"
    write_proposal(state, "svc", "broken", {"status": ["accepted"]})
    write_proposal(state, "svc", "good", {
        "status": "accepted",
        "control": {"requires_human_approval": True},
        "approval": {"approved_by": "a-person"},
    })

    refs = run_implementer.find_accepted_proposals(state)

    assert [r.slug for r in refs] == ["good"]
