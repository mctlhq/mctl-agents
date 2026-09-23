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
a bare id per loop without that memo and a `{workflow_id,
issue_workflow_id}` dict per loop with it; and every
consumer MATCHES on the alias but REPORTS the real id. Putting the alias into
the running set as if it were an id would have been wrong: the lifecycle
reconciler compares the id a record names (`dev-loop-xr_*`) with the live id,
and would escalate every dispatched loop as a `conflicting-owner`.

Temporalio-free on purpose: the activities that consume it are pure filters
over a list the workflow hands them, and their tests build that list by hand.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from orchestrator.temporal.issue_ref import is_dispatched_workflow_id

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
    """The running set, indexed for `owners_of`. Build with `index`.

    Immutable all the way down (tuples, not a dict), so `frozen` means what
    it says and the value hashes."""

    ids: frozenset[str] = frozenset()
    #: (issue-keyed id, the real ids of the dispatched loops that carry it),
    #: sorted by alias.
    aliases: tuple[tuple[str, tuple[str, ...]], ...] = ()
    #: Entries that may be a live loop this set cannot attribute to a
    #: proposal: a non-empty entry `index` could not read, and a dispatched
    #: `dev-loop-xr_*` loop with no alias (its memo was missing or unreadable,
    #: which the listing reports as a bare id). A caller whose safety argument
    #: IS the active set (the implement sweep) must treat a non-zero count as
    #: "possibly owned" and run nothing; the others log it.
    unreadable: int = 0

    def owners_of(self, issue_workflow_id: str | None) -> tuple[str, ...]:
        """Every running loop that works on the issue `issue_workflow_id`
        names, by its REAL id: the issue-keyed loop itself first (the answer
        these sweeps gave before #474), then any dispatched loop whose memo
        names that issue, sorted so the order does not depend on the listing.
        Empty when nothing runs for it.

        Deliberately no single-answer variant. More than one entry is a real
        state (two execution requests for one issue, or an issue-keyed and a
        dispatched loop together), and collapsing it to whichever id sorts
        first would name the wrong loop in evidence. Each caller decides what
        several owners mean for it."""
        if not issue_workflow_id:
            return ()
        owners: list[str] = []
        if issue_workflow_id in self.ids:
            owners.append(issue_workflow_id)
        for alias, real_ids in self.aliases:
            if alias == issue_workflow_id:
                owners.extend(o for o in real_ids if o != issue_workflow_id)
        return tuple(owners)


#: What one listing entry is on the wire, and what the consumer activities
#: declare so temporalio decodes both shapes: a bare workflow id for a loop
#: without an alias, a `{workflow_id, issue_workflow_id}` dict for one with.
ActiveLoopEntry = str | dict[str, str]


class _Unreadable(Exception):
    pass


def _entry(raw: Any) -> ActiveDevLoop | None:
    """One listing entry, in any shape a workflow history can hand back.

    A bare string is an issue-keyed loop (and every entry the listing
    returned before #474); a dict is a loop carrying the alias memo, after the
    JSON round trip through the workflow (which calls the activity by name,
    with no result type). None for an EMPTY entry, which names no loop;
    `_Unreadable` for anything else, which might.
    """
    if raw is None or raw == "":
        return None
    if isinstance(raw, ActiveDevLoop):
        return raw
    if isinstance(raw, str):
        return ActiveDevLoop(workflow_id=raw)
    if isinstance(raw, dict):
        workflow_id = raw.get("workflow_id")
        alias = raw.get("issue_workflow_id", "")
        if isinstance(workflow_id, str) and workflow_id and isinstance(alias, str):
            return ActiveDevLoop(workflow_id=workflow_id, issue_workflow_id=alias)
    raise _Unreadable


def index(entries: Iterable[Any] | None) -> ActiveLoops:
    """Index the listing activity's result for the consumers.

    Never raises on a bad entry: it is counted in `unreadable` instead, so
    each consumer decides how to fail (the implement sweep closed, the
    reporting sweeps by logging)."""
    ids: set[str] = set()
    aliases: dict[str, set[str]] = {}
    unreadable = 0
    for raw in entries or ():
        try:
            entry = _entry(raw)
        except _Unreadable:
            unreadable += 1
            continue
        if entry is None:
            continue
        ids.add(entry.workflow_id)
        if not entry.issue_workflow_id and is_dispatched_workflow_id(entry.workflow_id):
            # A dispatched loop the listing could not attribute (no memo, or
            # an unreadable one). Still a real id, for the sweeps that look
            # loops up by id, but it may own any proposal, so it is counted.
            unreadable += 1
        if entry.issue_workflow_id and entry.issue_workflow_id != entry.workflow_id:
            aliases.setdefault(entry.issue_workflow_id, set()).add(entry.workflow_id)
    return ActiveLoops(
        ids=frozenset(ids),
        aliases=tuple((alias, tuple(sorted(owners))) for alias, owners in sorted(aliases.items())),
        unreadable=unreadable,
    )
