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


def loop_workflow_id(issue_url: str, temporal_workflow_id: str | None = None) -> str:
    """The DevLoop an agent-container run belongs to: the id its loop passed
    (`--temporal-workflow-id`, mctlhq/mctl-agents#461), else the issue-keyed
    id `workflow_id_for` derives.

    Every DevLoop is issue-keyed (#461 option A: the execution-request
    dispatcher starts or joins `dev-loop-<owner>-<repo>-<n>` too), so the
    two agree; the passed id is still preferred because it is what the loop
    itself reported, and the one place both the approve instructions and the
    sealed correlation read it from, so they cannot disagree."""
    return temporal_workflow_id or workflow_id_for(issue_url)


#: Separates the loop's workflow id from the request id in the engine ref of
#: every execution the dispatcher fulfils. Never part of a workflow id:
#: Temporal ids here are `dev-loop-<owner>-<repo>-<n>`.
REQUEST_ENGINE_REF_SEPARATOR = "#"
#: mctl-api's `workitems.MaxEngineRefBytes`: a longer ref is refused (400).
MAX_ENGINE_REF_BYTES = 256


def request_engine_ref(loop_workflow_id: str, execution_request_id: str) -> str:
    """The `engine_ref` of the execution an mctl-api execution request runs
    as on the DevLoop `loop_workflow_id` (mctlhq/mctl-agents#461):
    `<loop id>#<request id>`, whether the request started that loop, started
    a continuation of it, or was delivered onto it while it ran.

    A pure function of (loop, request), so a re-claim of the same request
    fulfils with the same ref and gets the same `we_` back by mctl-api's
    `(engine, engine_ref)` idempotency. Unique per item, because a request
    id is. No run id in it, so it survives the loop's continue-as-new.

    NOT a workflow id: anything that turns a ledger entry into a Temporal
    handle must go through `loop_id_of_engine_ref`."""
    return f"{loop_workflow_id}{REQUEST_ENGINE_REF_SEPARATOR}{execution_request_id}"


def is_request_engine_ref(engine_ref: str) -> bool:
    """Was `engine_ref` written by the dispatcher for an execution request?
    The `#xr_` suffix is the proof: no workflow id has it."""
    return f"{REQUEST_ENGINE_REF_SEPARATOR}xr_" in engine_ref


def loop_id_of_engine_ref(engine_ref: str) -> str:
    """The DevLoop workflow id behind a Temporal execution's `engine_ref`:
    the ref itself, or its loop half for a dispatched request."""
    return engine_ref.split(REQUEST_ENGINE_REF_SEPARATOR, 1)[0]
