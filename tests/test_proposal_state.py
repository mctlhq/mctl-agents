"""Direct tests for orchestrator.proposal_state's read-modify-write helpers (#201).

The approval/authorization predicates are pinned elsewhere
(test_execution_authorization_contract.py, test_proposal_digest.py); this
covers load_status, update_status_file and the atomic writer.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
import yaml

from orchestrator import proposal_state as ps


def test_load_status_absent_is_empty(tmp_path: Path) -> None:
    assert ps.load_status(tmp_path / ".status.yaml") == {}


def test_load_status_rejects_a_non_mapping(tmp_path: Path) -> None:
    p = tmp_path / ".status.yaml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="expected a mapping"):
        ps.load_status(p)


def test_update_preserves_unrelated_fields_and_stamps_audit(tmp_path: Path) -> None:
    p = tmp_path / ".status.yaml"
    p.write_text(yaml.safe_dump({"status": "proposed", "keep": {"x": 1}, "drop": "y"}))
    out = ps.update_status_file(p, "in-progress", actor="tester", drop=None, new="v", skipped=ps.UNSET)
    on_disk = yaml.safe_load(p.read_text())
    assert on_disk == out
    assert on_disk["status"] == "in-progress"
    assert on_disk["keep"] == {"x": 1}
    assert "drop" not in on_disk and "skipped" not in on_disk
    assert on_disk["new"] == "v"
    assert on_disk["updated_by"] == "tester"
    assert on_disk["updated_at"].endswith("Z")


def test_update_creates_missing_file_and_parent(tmp_path: Path) -> None:
    p = tmp_path / "sub" / ".status.yaml"
    ps.update_status_file(p, "proposed")
    assert yaml.safe_load(p.read_text())["status"] == "proposed"


def test_accepting_with_unmet_approval_is_refused_and_leaves_file_untouched(tmp_path: Path) -> None:
    p = tmp_path / ".status.yaml"
    original = yaml.safe_dump({"status": "proposed", "control": {"requires_human_approval": True}})
    p.write_text(original)
    with pytest.raises(ps.UnrunnableProposalError):
        ps.update_status_file(p, "accepted")
    assert p.read_text() == original


def test_annotating_an_already_unrunnable_proposal_is_allowed(tmp_path: Path) -> None:
    p = tmp_path / ".status.yaml"
    p.write_text(yaml.safe_dump({"status": "accepted", "control": {"requires_human_approval": True}}))
    out = ps.update_status_file(p, "accepted", blocked={"code": ps.BLOCKED_APPROVAL_MISSING})
    assert out["blocked"]["code"] == ps.BLOCKED_APPROVAL_MISSING


def test_rewrite_keeps_file_mode_and_leaves_no_temp_files(tmp_path: Path) -> None:
    p = tmp_path / ".status.yaml"
    p.write_text("status: proposed\n")
    os.chmod(p, 0o640)
    ps.update_status_file(p, "accepted")
    assert stat.S_IMODE(os.stat(p).st_mode) == 0o640
    assert [f.name for f in tmp_path.iterdir()] == [".status.yaml"]


def test_serialisation_failure_leaves_original_intact(tmp_path: Path) -> None:
    p = tmp_path / ".status.yaml"
    p.write_text("status: proposed\n")
    with pytest.raises(Exception):  # noqa: B017 - any yaml representer error
        ps.update_status_file(p, "accepted", bad=object())
    assert p.read_text() == "status: proposed\n"
    assert [f.name for f in tmp_path.iterdir()] == [".status.yaml"]
