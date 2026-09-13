"""A service whose name starts with a dot must still be discovered.

`.github` is a registered service (config/settings.py), so its proposals
live at `agents-state/.github/proposals/<slug>/`. Every scanner that walks
that tree uses `Path.iterdir()`, which — unlike a shell glob — matches a
dotted directory. These tests pin that, because the failure mode is silent:
the investigator writes a proposal nobody ever picks up.
"""
from __future__ import annotations

from pathlib import Path

from orchestrator import run_implementer, run_shepherd

SERVICE = ".github"
SLUG = "issue-67-roadmap-reconciler"


def _proposal(state_dir: Path, status: str, pr: str | None = None) -> Path:
    proposal_dir = state_dir / SERVICE / "proposals" / SLUG
    proposal_dir.mkdir(parents=True)
    body = f"status: {status}\n"
    if pr is not None:
        body += f"pr: {pr}\n"
    (proposal_dir / ".status.yaml").write_text(body, encoding="utf-8")
    return proposal_dir


def test_implementer_discovers_a_dotted_service(tmp_path: Path) -> None:
    """find_accepted_proposals must see agents-state/.github/."""
    _proposal(tmp_path, "accepted")

    refs = run_implementer.find_accepted_proposals(tmp_path)

    assert [(r.service, r.slug) for r in refs] == [(SERVICE, SLUG)]


def test_implementer_service_filter_accepts_a_dotted_name(tmp_path: Path) -> None:
    """`--service .github` must narrow to it rather than to nothing."""
    _proposal(tmp_path, "accepted")

    refs = run_implementer.find_accepted_proposals(tmp_path, service_filter=SERVICE)

    assert [r.service for r in refs] == [SERVICE]


def test_shepherd_discovers_a_dotted_service(tmp_path: Path) -> None:
    """_discover_refs must see agents-state/.github/ too — it owns the PR loop."""
    _proposal(tmp_path, "implemented", pr="https://github.com/mctlhq/.github/pull/1")

    refs = run_shepherd._discover_refs(tmp_path)

    assert [(r.service, r.slug) for r in refs] == [(SERVICE, SLUG)]
