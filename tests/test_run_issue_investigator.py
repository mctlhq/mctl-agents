"""Unit tests for the issue-investigator (``orchestrator.run_issue_investigator``)
and the .status.yaml `source`-block preservation it depends on.

The SDK / clone / gh-comment paths are not exercised here — the tests hit
the pure helpers (URL parsing, slug, status IO) and the idempotency guard
in ``investigate`` via a mocked ``gh_issue_view``.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from orchestrator.run_issue_investigator import (
    IssueData,
    IssueRef,
    build_slug,
    gh_issue_view,
    investigate,
    parse_issue_url,
    slugify,
    write_status_yaml,
)
from orchestrator.run_implementer import ProposalRef, update_status_yaml


# ---------------------------------------------------------------------------
# parse_issue_url
# ---------------------------------------------------------------------------
def test_parse_issue_url_ok():
    ref = parse_issue_url("https://github.com/mctlhq/mctl-telegram/issues/123")
    assert ref.owner == "mctlhq"
    assert ref.repo == "mctl-telegram"
    assert ref.number == 123
    assert ref.full_repo == "mctlhq/mctl-telegram"


def test_parse_issue_url_trailing_slash_and_http():
    ref = parse_issue_url("http://github.com/mctlhq/mctl-api/issues/7/")
    assert ref.repo == "mctl-api"
    assert ref.number == 7


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/mctlhq/mctl-api/pull/7",          # a PR, not an issue
        "https://github.com/mctlhq/mctl-api/issues/abc",      # non-numeric
        "https://gitlab.com/mctlhq/mctl-api/issues/7",        # wrong host
        "mctlhq/mctl-api#7",                                  # shorthand, not a URL
        "",
    ],
)
def test_parse_issue_url_malformed(url):
    with pytest.raises(SystemExit):
        parse_issue_url(url)


def test_parse_issue_url_rejects_non_mctlhq_owner():
    with pytest.raises(SystemExit):
        parse_issue_url("https://github.com/someone-else/mctl-api/issues/7")


# ---------------------------------------------------------------------------
# slugify / build_slug
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text,want",
    [
        ("Add observability for mctl-telegram", "add-observability-for-mctl-telegram"),
        ("  Spaces   and   PUNCT!!! ", "spaces-and-punct"),
        ("CamelCase/Slashes", "camelcase-slashes"),
        ("", "untitled"),
        ("!!!", "untitled"),
    ],
)
def test_slugify(text, want):
    assert slugify(text) == want


def test_slugify_truncates_and_trims_trailing_dash():
    out = slugify("a" * 30 + " " + "b" * 30, max_len=40)
    assert len(out) <= 40
    assert not out.endswith("-")


def test_build_slug_deterministic():
    title = "Add observability and alerting for mctl-telegram"
    a = build_slug(123, title)
    b = build_slug(123, title)
    assert a == b
    assert a.startswith("issue-123-")


# ---------------------------------------------------------------------------
# write_status_yaml
# ---------------------------------------------------------------------------
def _issue(number=123, title="Add monitoring", repo="mctl-telegram"):
    ref = IssueRef(
        owner="mctlhq",
        repo=repo,
        number=number,
        url=f"https://github.com/mctlhq/{repo}/issues/{number}",
    )
    return IssueData(ref=ref, title=title, body="body", state="OPEN")


def test_write_status_yaml_shape(tmp_path):
    proposal_dir = tmp_path / "proposals" / "issue-123-add-monitoring"
    status_path = write_status_yaml(proposal_dir, _issue())

    data = yaml.safe_load(status_path.read_text())
    assert data["status"] == "proposed"
    assert data["updated_by"] == "mctl-agents[bot]"
    assert data["source"] == {
        "type": "github_issue",
        "repo": "mctlhq/mctl-telegram",
        "issue": 123,
        "url": "https://github.com/mctlhq/mctl-telegram/issues/123",
    }
    assert data["control"]["requires_human_approval"] is True


# ---------------------------------------------------------------------------
# update_status_yaml preserves the unmanaged `source` block
# ---------------------------------------------------------------------------
def test_update_status_yaml_preserves_source(tmp_path):
    """The implementer's status transitions must not drop the issue link."""
    proposal_dir = tmp_path / "proposals" / "issue-123-add-monitoring"
    write_status_yaml(proposal_dir, _issue())

    ref = ProposalRef(
        service="mctl-telegram",
        slug="issue-123-add-monitoring",
        proposal_dir=proposal_dir,
        status="accepted",
    )
    # Simulate the accepted → in-progress → implemented walk.
    update_status_yaml(ref, "in-progress")
    update_status_yaml(ref, "implemented", pr="https://github.com/mctlhq/mctl-telegram/pull/9")

    data = yaml.safe_load((proposal_dir / ".status.yaml").read_text())
    assert data["status"] == "implemented"
    assert data["pr"].endswith("/pull/9")
    # The unmanaged blocks survived every rewrite.
    assert data["source"]["issue"] == 123
    assert data["source"]["repo"] == "mctlhq/mctl-telegram"
    assert data["control"]["requires_human_approval"] is True


# ---------------------------------------------------------------------------
# investigate — idempotency guard
# ---------------------------------------------------------------------------
def test_investigate_skips_proposal_past_proposed(tmp_path, monkeypatch):
    """A proposal already owned by the implementer must not be clobbered."""
    issue = _issue(number=42, title="Add monitoring")
    monkeypatch.setattr(
        "orchestrator.run_issue_investigator.gh_issue_view",
        lambda url: issue,
    )

    slug = build_slug(42, "Add monitoring")
    proposal_dir = tmp_path / "mctl-telegram" / "proposals" / slug
    write_status_yaml(proposal_dir, issue)
    # Move it past `proposed` — an implementation is now in flight.
    (proposal_dir / ".status.yaml").write_text(
        yaml.safe_dump({"status": "implemented"})
    )

    result = investigate("https://github.com/mctlhq/mctl-telegram/issues/42", tmp_path)
    assert result.skipped_reason is not None
    assert "in-flight" in result.skipped_reason
    assert result.error is None


def test_investigate_dry_run_does_not_skip_fresh_issue(tmp_path, monkeypatch):
    issue = _issue(number=42, title="Add monitoring")
    monkeypatch.setattr(
        "orchestrator.run_issue_investigator.gh_issue_view",
        lambda url: issue,
    )
    result = investigate(
        "https://github.com/mctlhq/mctl-telegram/issues/42",
        tmp_path,
        dry_run=True,
    )
    assert result.skipped_reason == "dry-run"
    assert result.error is None


def test_investigate_rejects_unknown_service(tmp_path, monkeypatch):
    issue = _issue(number=1, title="x", repo="not-a-service")
    monkeypatch.setattr(
        "orchestrator.run_issue_investigator.gh_issue_view",
        lambda url: issue,
    )
    with pytest.raises(SystemExit):
        investigate("https://github.com/mctlhq/not-a-service/issues/1", tmp_path)


# Keep an explicit reference so an accidental removal of the public helper
# trips the import at collection time.
assert callable(gh_issue_view)
