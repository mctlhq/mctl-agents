"""Tests for orchestrator/temporal/issue_ref.py."""
from __future__ import annotations

import pytest

from orchestrator.temporal.issue_ref import parse_issue_url


def test_parses_well_formed_issue_url():
    parts = parse_issue_url("https://github.com/mctlhq/mctl-telegram/issues/296")
    assert parts.owner == "mctlhq"
    assert parts.repo == "mctl-telegram"
    assert parts.number == "296"


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/other-org/mctl-telegram/issues/1",
        "https://github.com/mctlhq/mctl-telegram/pull/1",
        "https://gitlab.com/mctlhq/mctl-telegram/issues/1",
        "not-a-url",
        "",
    ],
)
def test_rejects_malformed_urls(url):
    with pytest.raises(ValueError, match="does not look like"):
        parse_issue_url(url)
