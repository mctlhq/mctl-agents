"""Parse a `https://github.com/mctlhq/<repo>/issues/<N>` URL into its parts.

Shared by cli.py (workflow-ID derivation) and workflows/dev_loop.py (deriving
the target repo for implement-step scoping and the execution record) so the
mctlhq-only, issues-only URL shape is validated in exactly one place.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

_ISSUE_URL_RE = re.compile(r"^https://github\.com/mctlhq/([A-Za-z0-9_.-]+)/issues/([0-9]+)$")


@dataclass(frozen=True)
class IssueRefParts:
    owner: str
    repo: str
    number: str


def parse_issue_url(issue_url: str) -> IssueRefParts:
    match = _ISSUE_URL_RE.match(issue_url)
    if not match:
        raise ValueError(f"{issue_url!r} does not look like a mctlhq GitHub issue URL")
    repo, number = match.groups()
    return IssueRefParts(owner="mctlhq", repo=repo, number=number)
