"""The running-DevLoop set, keyed by the issue each loop works on
(mctlhq/mctl-agents#474).

Three sweeps ask "is a DevLoop running for this proposal?" by rebuilding the
issue-keyed id `dev-loop-<owner>-<repo>-<n>` from the proposal slug
(`orphans.expected_dev_loop_id`) and looking it up in the running set: the
implement sweep (`stranded.py`), the orphan sweep (`orphans.py`) and the
lifecycle reconcile (`lifecycle_reconcile._live_id`). A loop the
execution-request dispatcher started is `dev-loop-xr_<request id>`, so that
lookup never finds it: the implement sweep would run the implementer a second
time on a proposal whose dispatched loop is still queued.

The fix is an alias, not a second id in the running set. The dispatcher's
start (`start.start_dispatched_dev_loop`) records the issue-keyed id in the
workflow's memo under `ISSUE_WORKFLOW_ID_MEMO`; the listing activity returns
one `{workflow_id, issue_workflow_id}` entry per running loop; and every
consumer MATCHES on the alias but REPORTS the real id. Putting the alias into
the running set as if it were an id would have been wrong: the lifecycle
reconciler compares the id a record names (`dev-loop-xr_*`) with the live id,
and would escalate every dispatched loop as a `conflicting-owner`.

Temporalio-free on purpose: the activities that consume it are pure filters
over a list the workflow hands them, and their tests build that list by hand.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

#: The memo key `start_dispatched_dev_loop` writes the issue-keyed workflow id
#: under. A memo, not a search attribute: it is returned with every
#: `list_workflows` row without registering anything on the Temporal server,
#: and nothing needs to FILTER on it — the listing already selects every
#: running DevLoop, and the match happens in the consumer.
ISSUE_WORKFLOW_ID_MEMO = "issue_workflow_id"


@dataclass(frozen=True)
class ActiveDevLoop:
    """One running DevLoop, as the listing activity reports it.

    `issue_workflow_id` is the issue-keyed id the loop stands in for: the
    memo a dispatched loop carries, or "" for an issue-keyed loop (whose own
    id already IS the issue-keyed id) and for anything started without it.
    """

    workflow_id: str
    issue_workflow_id: str = ""


@dataclass(frozen=True)
class ActiveLoops:
    """The running set, indexed for `owners_of`. Build with `index`."""

    ids: frozenset[str] = frozenset()
    by_alias: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def owners_of(self, issue_workflow_id: str | None) -> tuple[str, ...]:
        """Every running loop that works on the issue `issue_workflow_id`
        names, by its REAL id: the issue-keyed loop itself first (the answer
        these sweeps gave before #474), then any dispatched loop whose memo
        names that issue, sorted so the order does not depend on the listing.
        Empty when nothing runs for it."""
        if not issue_workflow_id:
            return ()
        owners: list[str] = []
        if issue_workflow_id in self.ids:
            owners.append(issue_workflow_id)
        owners.extend(o for o in self.by_alias.get(issue_workflow_id, ()) if o != issue_workflow_id)
        return tuple(owners)

    def owner_of(self, issue_workflow_id: str | None) -> str:
        """The first of `owners_of`, or ""."""
        owners = self.owners_of(issue_workflow_id)
        return owners[0] if owners else ""


def _entry(raw: Any) -> ActiveDevLoop | None:
    """One listing entry, in any shape a workflow history can hand back.

    A bare string is what `list_active_dev_loop_ids` returned before #474;
    a reconcile or sweep tick that recorded that result before the deploy and
    replays its next activity on the new worker hands those strings through
    unchanged. A dict is the new entry after the JSON round trip through the
    workflow (which calls the activity by name, with no result type).
    """
    if isinstance(raw, ActiveDevLoop):
        return raw
    if isinstance(raw, str):
        return ActiveDevLoop(workflow_id=raw) if raw else None
    if isinstance(raw, dict):
        workflow_id = raw.get("workflow_id")
        if not isinstance(workflow_id, str) or not workflow_id:
            return None
        alias = raw.get("issue_workflow_id")
        return ActiveDevLoop(workflow_id=workflow_id, issue_workflow_id=alias if isinstance(alias, str) else "")
    return None


def index(entries: Iterable[Any] | None) -> ActiveLoops:
    """Index the listing activity's result for the consumers."""
    ids: set[str] = set()
    aliases: dict[str, set[str]] = {}
    for raw in entries or ():
        entry = _entry(raw)
        if entry is None:
            continue
        ids.add(entry.workflow_id)
        if entry.issue_workflow_id and entry.issue_workflow_id != entry.workflow_id:
            aliases.setdefault(entry.issue_workflow_id, set()).add(entry.workflow_id)
    return ActiveLoops(
        ids=frozenset(ids),
        by_alias={alias: tuple(sorted(owners)) for alias, owners in aliases.items()},
    )
