"""T10-T11: issue-596 conversion, loss report and round trip (spike #655)."""

from pathlib import Path

import yaml

from orchestrator.sdd.convert import TRIPLET, change_to_triplet, loss_report, triplet_to_change
from orchestrator.sdd.measure import ESTIMATE_METHOD, measure

D = Path(__file__).parent / "fixtures" / "sdd" / "roundtrip" / "issue-596"


def _load():
    triplet = {n: (D / n).read_text() for n in TRIPLET}
    info = yaml.safe_load((D / "source.yaml").read_text())
    return triplet, info


def _pkg():
    t, info = _load()
    return t, info, triplet_to_change(t, info, capability="authoring-canary", created="2026-10-16")


def test_t10_loss_report_complete_and_source_linked():
    t, info, pkg = _pkg()
    report = loss_report(t, info, pkg)
    assert report and [e for e in report if e.target is None] == []
    assert {"heading", "bullet", "checkbox", "source"} <= {e.kind for e in report}
    meta = yaml.safe_load(pkg.files[".openspec.yaml"])
    assert meta["x-mctl-issue"] == info["source"]["url"]
    assert "specs/authoring-canary/spec.md" in pkg.files
    ears = [e for e in report if e.file == "requirements.md" and e.text.startswith("- WHEN")]
    assert ears and all(e.target == "specs/authoring-canary/spec.md" for e in ears)


def test_t10_no_execution_state_leaks():
    _, _, pkg = _pkg()
    blob = "\n".join(pkg.files.values())
    assert "approved_by" not in blob and "trace_id" not in blob


def test_t11_roundtrip_stable():
    t, info, pkg = _pkg()
    t2, info2 = change_to_triplet(pkg)
    assert info2 == info
    for name in TRIPLET:
        assert t2[name] == t[name]  # byte-exact on this fixture


def test_sizes_recorded():
    t, _, pkg = _pkg()
    cur, cand = measure(t), measure(pkg.files)
    assert cur.bytes > 0 and cand.bytes > cur.bytes * 0.5
    assert cur.approx_tokens > 0 and "len(text) / 4" in ESTIMATE_METHOD


def test_ac_section_without_bullets_is_not_dropped():
    import pytest

    from orchestrator.sdd.convert import ConversionError

    t, info = _load()
    t["requirements.md"] = "# T\n\n## Acceptance Criteria (EARS)\n\n1. WHEN a THEN THE SYSTEM SHALL b\n"
    with pytest.raises(ConversionError, match="bullet"):
        triplet_to_change(t, info, capability="c", created="2026-10-16")


def test_null_source_block_is_tolerated():
    t, info = _load()
    info["source"] = None
    pkg = triplet_to_change(t, info, capability="c", created="2026-10-16")
    assert "x-mctl-issue" not in yaml.safe_load(pkg.files[".openspec.yaml"])
    assert [e for e in loss_report(t, info, pkg) if e.kind == "source"]
