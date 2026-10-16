"""T8-T9: delta semantics against the pinned archive rules (spike #655)."""

import datetime as dt
import json
import shutil
from pathlib import Path

import pytest

from orchestrator.sdd.apply import ApplyAbort, apply_change, archive_change

FIX = Path(__file__).parent / "fixtures" / "sdd"
ROWS = sorted(p for p in (FIX / "delta-rows").iterdir() if p.is_dir())


@pytest.mark.parametrize("row", ROWS, ids=lambda p: p.name)
def test_t8_delta_row(row):
    meta = json.loads((row / "expected.json").read_text())
    cap = meta["cap"]
    canonical = {cap: (row / "spec.md").read_text()} if (row / "spec.md").exists() else {}
    before = dict(canonical)
    delta = {cap: (row / "delta.md").read_text()}
    if meta["outcome"] == "abort":
        with pytest.raises(ApplyAbort):
            apply_change(canonical, delta, mode=meta["mode"])
        assert canonical == before  # byte-identical input
        return
    res = apply_change(canonical, delta, mode=meta["mode"])
    assert res.specs[cap] == (row / "expected.md").read_text()
    if meta["warning"]:
        assert any(meta["warning"] in w for w in res.warnings)
    else:
        assert res.warnings == []
    assert canonical == before


def _stage(tmp_path: Path) -> Path:
    root = tmp_path / "openspec"
    shutil.copytree(FIX / "capabilities", root / "specs")
    shutil.copytree(
        FIX / "changes" / "devloop-approval-content-digest",
        root / "changes" / "devloop-approval-content-digest",
    )
    shutil.rmtree(root / "changes" / "devloop-approval-content-digest" / "expected")
    return root


def test_t9_canonical_before_delta_after_and_archive_once(tmp_path):
    root = _stage(tmp_path)
    spec = root / "specs" / "devloop-approval" / "spec.md"
    expected = (FIX / "changes/devloop-approval-content-digest/expected/spec.md").read_text()
    before = spec.read_text()
    assert "Legacy manual approval flip" in before and "Approval records proposal digest" not in before
    day = dt.date(2026, 10, 16)
    target = archive_change(root, "devloop-approval-content-digest", today=day)
    assert target.name == "2026-10-16-devloop-approval-content-digest"
    assert spec.read_text() == expected
    assert "Legacy manual approval flip" not in expected
    assert not (root / "changes" / "devloop-approval-content-digest").exists()
    # re-archiving fails: change moved, and the archive target exists
    shutil.copytree(target, root / "changes" / "devloop-approval-content-digest")
    with pytest.raises(ApplyAbort, match="already exists"):
        archive_change(root, "devloop-approval-content-digest", today=day)
    # idempotency is per requirement: the same delta on the new canonical is a no-op
    delta = (target / "specs/devloop-approval/spec.md").read_text()
    again = apply_change({"devloop-approval": expected}, {"devloop-approval": delta})
    assert again.specs["devloop-approval"] == expected
    assert any("Legacy manual approval flip" in w for w in again.warnings)


def test_abort_does_not_archive_or_touch_specs(tmp_path):
    root = _stage(tmp_path)
    delta = root / "changes/devloop-approval-content-digest/specs/devloop-approval/spec.md"
    delta.write_text("## MODIFIED Requirements\n\n### Requirement: Nope\nx\n")
    spec = root / "specs/devloop-approval/spec.md"
    before = spec.read_bytes()
    with pytest.raises(ApplyAbort):
        archive_change(root, "devloop-approval-content-digest", today=dt.date(2026, 10, 16))
    assert spec.read_bytes() == before
    assert not (root / "changes" / "archive").exists()
    assert delta.exists()
