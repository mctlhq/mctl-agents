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


def workflow_id_for(issue_url: str) -> str:
    # dev-loop-{owner}-{repo}-{issue} — Temporal's own workflow-ID dedup
    # replaces the hand-rolled "already past proposed" guard the cron
    # pipeline needed pre-Temporal. Lives here (not start.py) so callers
    # that only need the id string — e.g. the investigator's issue comment,
    # which runs inside the agent container — never import temporalio.
    parts = parse_issue_url(issue_url)
    return f"dev-loop-{parts.owner}-{parts.repo}-{parts.number}"


#: Every dispatched DevLoop's workflow id starts with this: `dev-loop-`
#: followed by mctl-api's `xr_` request-id prefix. An issue-keyed loop is
#: `dev-loop-<owner>-...` and never matches.
DISPATCHED_WORKFLOW_PREFIX = "dev-loop-xr_"


def is_dispatched_workflow_id(workflow_id: str) -> bool:
    """Was `workflow_id` started by the execution-request dispatcher?"""
    return workflow_id.startswith(DISPATCHED_WORKFLOW_PREFIX)


def dispatched_workflow_id(execution_request_id: str) -> str:
    """The DevLoop workflow id for one mctl-api execution request
    (mctlhq/mctl-agents#461): a pure function of the request id, so every
    claim of the same request — the first, or a re-claim after a crash and
    a lapsed lease, under a new claim token — names the same run. It is
    also the `engine_ref` the dispatcher fulfils the request with, so the
    same run is the same `we_` execution by mctl-api's
    `(engine, engine_ref)` idempotency."""
    return f"dev-loop-{execution_request_id}"
