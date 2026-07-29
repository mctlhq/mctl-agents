"""Attributable progress-log tests for implementer runs."""
from pathlib import Path

from orchestrator import run_implementer


def _ref(slug: str) -> run_implementer.ProposalRef:
    return run_implementer.ProposalRef(
        service="mctl-agents",
        slug=slug,
        proposal_dir=Path("/tmp") / slug,
        status="accepted",
    )


def test_progress_markers_identify_each_proposal(monkeypatch, capsys) -> None:
    refs = [_ref("first"), _ref("second")]

    def fake_implement(ref, dry_run=False):
        if ref.slug == "first":
            return run_implementer.ImplementResult(
                ref=ref,
                pr_url="https://github.com/mctlhq/mctl-agents/pull/1",
                counts_toward_limit=False,
            )
        return run_implementer.ImplementResult(
            ref=ref,
            pr_url=None,
            error="preflight unavailable",
            counts_toward_limit=False,
        )

    monkeypatch.setattr(run_implementer, "implement_one", fake_implement)
    run_implementer._implement_refs(refs, max_proposals=1, dry_run=False)

    output = capsys.readouterr().out
    assert "[mctl-agents/first] Implementing" in output
    assert "[mctl-agents/first] Finished: ready" in output
    assert "[mctl-agents/second] Implementing" in output
    assert "[mctl-agents/second] Finished: failed" in output
