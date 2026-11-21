"""#281/#405: one proposal whose PR lookup fails transiently must not abort
the whole sweep, and must not be reported as drift or as an orphan."""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import yaml

from orchestrator import run_shepherd
from orchestrator.temporal.activities import discovery, orphans
from orchestrator.temporal.workflows.reconcile import ACTIVITY_RETRY_POLICY

PR_URL = "https://github.com/mctlhq/mctl-web/pull/{n}"


def _state(tmp_path: Path, slugs: list[str]) -> None:
    for n, slug in enumerate(slugs, start=1):
        d = tmp_path / "mctl-web" / "proposals" / slug
        d.mkdir(parents=True)
        (d / ".status.yaml").write_text(
            yaml.safe_dump({"status": "implemented", "pr": PR_URL.format(n=n)}),
            encoding="utf-8",
        )


def _flaky(bad_slug: str, good):
    def fake(service, slug, **_kw):
        if slug == bad_slug:
            raise run_shepherd.PRDiscoveryTransientError("gh down")
        return good

    return fake


def test_discovery_skips_transient_item_and_keeps_sweeping(tmp_path, monkeypatch) -> None:
    _state(tmp_path, ["a-bad", "b-good"])
    from tests.test_run_shepherd import make_pr

    pr = make_pr(merged=True, merge_commit="c" * 40)
    monkeypatch.setattr(discovery, "find_pr_for_proposal", _flaky("a-bad", pr))
    result = discovery._sync_discover_and_project(tmp_path)
    assert [p.slug for p in result.projections] == ["b-good"]


def test_orphans_skip_transient_item_not_reported(tmp_path, monkeypatch) -> None:
    _state(tmp_path, ["a-bad", "b-good"])
    from tests.test_run_shepherd import make_pr

    pr = make_pr(merged=False, closed_unmerged=False)
    monkeypatch.setattr(orphans, "find_pr_for_proposal", _flaky("a-bad", pr))
    result = orphans._sync_detect_orphans(tmp_path, set())
    assert [o.slug for o in result.orphans] == ["b-good"]


def test_retry_window_spans_more_than_a_github_blip() -> None:
    p = ACTIVITY_RETRY_POLICY
    assert p.maximum_attempts >= 5
    assert p.initial_interval >= timedelta(seconds=5)
    assert p.backoff_coefficient > 1
