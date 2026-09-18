"""The reconcile sweep's I/O half (#353), driven over a faked transport.

The decision table is tested separately and purely. What is left here is
everything the pure module cannot see, and where every previous phase's
findings landed: the rollout gate, the batched read, and whether a decision
actually turns into the right sequence of writes at the right epoch.
"""
from __future__ import annotations

import json
from dataclasses import replace
from typing import Any

import httpx
import pytest
from temporalio.testing import ActivityEnvironment

from orchestrator.lifecycle import rollout
from orchestrator.temporal.activities import lifecycle as ownership_act
from orchestrator.temporal.activities import lifecycle_reconcile as act
from orchestrator.temporal.activities.gitops_state import ProposalStateRef, PRSnapshot

pytestmark = pytest.mark.anyio

_REAL_ASYNC_CLIENT = httpx.AsyncClient

REPO = "mctlhq/mctl-web"
SLUG = "issue-10-widget"
PR_ID = f"{REPO}#42"
PROPOSAL_ID = f"mctl-web/{SLUG}"
# What orphans._expected_workflow_id reconstructs for this proposal.
LIVE_WORKFLOW_ID = "dev-loop-mctlhq-mctl-web-10"


def _refs() -> list[ProposalStateRef]:
    return [
        ProposalStateRef(
            service="mctl-web",
            slug=SLUG,
            status="in-progress",
            pr_url=f"https://github.com/{REPO}/pull/42",
        )
    ]


def _snapshots(**kw: Any) -> dict[tuple[str, str], PRSnapshot]:
    base = dict(repo=REPO, number=42, merged=False, closed_unmerged=False, head_sha="abc1234")
    base.update(kw)
    return {("mctl-web", SLUG): PRSnapshot(**base)}


def _record(**kw: Any) -> dict[str, Any]:
    rec = {
        "entity": {"kind": "pull-request", "id": PR_ID, "version": "abc1234"},
        "phase": "review-remediation",
        "owner": {"type": "devloop-workflow", "id": "dev-loop-mctlhq-mctl-web-10"},
        "epoch": 3,
        "state": "active",
        "healthy": True,
        "derived": {"status": "healthy", "held": True},
    }
    rec.update(kw)
    return rec


class _Fake:
    """One handler standing in for mctl-api: a batch read plus the writes."""

    def __init__(self, ownership: dict[str, dict[str, Any]], write_status: int = 200):
        self.ownership = ownership
        self.write_status = write_status
        self.writes: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "GET":
            ids = request.url.params.get_list("id")
            found = {i: self.ownership[i] for i in ids if i in self.ownership}
            return httpx.Response(200, content=json.dumps({"ownership": found}).encode())
        body = json.loads(request.content or b"{}")
        self.writes.append((path, body))
        if self.write_status >= 400:
            return httpx.Response(
                self.write_status,
                content=json.dumps({"code": "owner-alive", "error": "owner is alive"}).encode(),
            )
        # A successful recovery hands the row over at epoch+1, which is what
        # the follow-up write must then assert.
        return httpx.Response(
            200,
            content=json.dumps(
                _record(
                    owner={"type": "reconciler", "id": "reconcile-1"},
                    epoch=body.get("epoch", 0) + 1,
                )
            ).encode(),
        )


def _wire(monkeypatch, fake, *, mode: str, refs=None, snapshots=None) -> None:
    """Point the activity at `fake` — a _Fake, or any bare handler."""
    monkeypatch.setenv(rollout.ENV_VAR, mode)
    monkeypatch.setattr(act, "auth_headers", lambda: {"Authorization": "Bearer test"})
    monkeypatch.setattr(ownership_act, "auth_headers", lambda: {"Authorization": "Bearer test"})

    async def _list_refs():
        return _refs() if refs is None else refs

    async def _fetch(_refs_arg):
        return _snapshots() if snapshots is None else snapshots

    monkeypatch.setattr(act, "list_proposal_refs", _list_refs)
    monkeypatch.setattr(act, "fetch_pr_snapshots", _fetch)

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = httpx.MockTransport(fake)
        return _REAL_ASYNC_CLIENT(*args, **kwargs)

    monkeypatch.setattr(act.httpx, "AsyncClient", _factory)
    monkeypatch.setattr(ownership_act.httpx, "AsyncClient", _factory)


async def _run(fake: _Fake, monkeypatch, *, mode: str = rollout.ENFORCE, active=None, **kw):
    _wire(monkeypatch, fake, mode=mode, **kw)
    return await ActivityEnvironment().run(
        act.reconcile_lifecycle_ownership, active or []
    )


def _finding(result, entity_id: str):
    return next(f for f in result.findings if f.entity_id == entity_id)


async def test_off_short_circuits_before_any_read(monkeypatch):
    """Break-glass OFF means nothing is read either, so a skipped tick cannot
    be mistaken for a clean one."""
    fake = _Fake({})
    result = await _run(fake, monkeypatch, mode=rollout.OFF)
    assert result.examined == 0
    assert "rollout mode off" in result.skipped_reason
    assert fake.writes == []


async def test_observe_classifies_but_writes_nothing(monkeypatch):
    """Below enforce the new answer decides nothing (ADR-010 §12) — and a
    recovery is the most consequential write in this contract."""
    fake = _Fake({PR_ID: _record(healthy=False, dead=True, derived={"status": "dead", "held": True})})
    result = await _run(fake, monkeypatch, mode=rollout.OBSERVE)
    finding = _finding(result, PR_ID)
    assert finding.action == "recover"
    assert finding.outcome == "observed"
    assert result.applied == 0
    assert fake.writes == []


async def test_a_healthy_owner_is_left_alone(monkeypatch):
    fake = _Fake({PR_ID: _record()})
    result = await _run(
        fake, monkeypatch, active=["dev-loop-mctlhq-mctl-web-10"]
    )
    assert _finding(result, PR_ID).action == "none"
    assert fake.writes == []
    assert result.applied == 0


async def test_dead_owner_is_recovered_then_released(monkeypatch):
    fake = _Fake({PR_ID: _record(healthy=False, dead=True, derived={"status": "dead", "held": True})})
    result = await _run(fake, monkeypatch)

    paths = [p for p, _ in fake.writes]
    assert paths == [
        "/api/v1/lifecycle/ownership/recover",
        "/api/v1/lifecycle/ownership/release",
    ]
    recover_body = fake.writes[0][1]
    # The epoch READ, so a row that moved underneath the sweep fails 412.
    assert recover_body["epoch"] == 3
    assert recover_body["owner_type"] == "reconciler"
    assert recover_body["evidence"]
    # The head the sweep OBSERVED, not the one the record carried.
    assert recover_body["version"] == "abc1234"
    # The release asserts the epoch the SERVER granted, not the one asked
    # with: at the old generation it would 412 and leave the row naming an
    # actor that does no work.
    assert fake.writes[1][1]["epoch"] == 4
    assert _finding(result, PR_ID).outcome == "applied"
    assert result.applied == 1


async def test_a_dead_owner_on_a_merged_pr_is_closed_not_released(monkeypatch):
    fake = _Fake({PR_ID: _record(healthy=False, dead=True, derived={"status": "dead", "held": True})})
    result = await _run(fake, monkeypatch, snapshots=_snapshots(merged=True))
    assert [p for p, _ in fake.writes] == [
        "/api/v1/lifecycle/ownership/recover",
        "/api/v1/lifecycle/ownership/terminal",
    ]
    assert "recovered and closed" in _finding(result, PR_ID).evidence


class _FailFollowUp(_Fake):
    """Recovery lands; the write that gives the row back does not."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET" and not request.url.path.endswith("/recover"):
            self.writes.append((request.url.path, json.loads(request.content or b"{}")))
            return httpx.Response(
                503, content=json.dumps({"code": "unavailable", "error": "store down"}).encode()
            )
        return super().__call__(request)


async def test_a_refused_release_is_reported_as_still_held(monkeypatch):
    """The reconciler recovered the row and then failed to give it back, so
    the entity is held by an actor that does no work. It self-heals within one
    liveness bound, but a reader grouping on `outcome` has to be able to see
    it — `applied` alone would say the opposite."""
    fake = _FailFollowUp(
        {PR_ID: _record(healthy=False, dead=True, derived={"status": "dead", "held": True})}
    )
    result = await _run(fake, monkeypatch)
    assert [p for p, _ in fake.writes] == [
        "/api/v1/lifecycle/ownership/recover",
        "/api/v1/lifecycle/ownership/release",
    ]
    finding = _finding(result, PR_ID)
    assert finding.outcome.startswith(act.OUTCOME_HELD)
    assert "release was refused" in finding.evidence
    # The recovery itself did happen, and the count is of accepted writes.
    assert result.applied == 1


async def test_a_refused_recovery_is_not_counted_as_applied(monkeypatch):
    """409 ErrOwnerAlive: the server re-derived liveness and the owner is not
    dead after all. Reporting that as a takeover would be reporting a write
    that did not happen."""
    fake = _Fake(
        {PR_ID: _record(healthy=False, dead=True, derived={"status": "dead", "held": True})},
        write_status=409,
    )
    result = await _run(fake, monkeypatch)
    assert [p for p, _ in fake.writes] == ["/api/v1/lifecycle/ownership/recover"]
    assert result.applied == 0
    assert _finding(result, PR_ID).outcome.startswith("refused")


async def test_incomplete_handoff_is_completed_for_the_named_target(monkeypatch):
    fake = _Fake(
        {
            PR_ID: _record(
                state="handing-off",
                handoff_to={"type": "shepherd", "id": "shepherd"},
                derived={"status": "handing-off", "held": True},
            )
        }
    )
    result = await _run(fake, monkeypatch)
    path, body = fake.writes[0]
    assert path == "/api/v1/lifecycle/ownership/handoff/complete"
    assert (body["owner_type"], body["owner_id"]) == ("shepherd", "shepherd")
    # A shepherd has no Temporal execution, so correlating the row with this
    # sweep's workflow would explain the new owner with somebody else's reason.
    assert "temporal_workflow_id" not in body
    assert result.applied == 1


async def test_a_store_outage_writes_nothing(monkeypatch):
    """Every id UNKNOWN, not UNOWNED — the one wrong reading that licenses
    action."""

    def _down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, content=b'{"error":"upstream down"}')

    _wire(monkeypatch, _down, mode=rollout.ENFORCE)
    result = await ActivityEnvironment().run(act.reconcile_lifecycle_ownership, [])
    assert result.examined == 2
    assert result.applied == 0
    assert all(f.action == "skip" for f in result.findings)
    assert all(f.reason == "store-unknown" for f in result.findings)


async def test_zero_owner_escalates_without_planting_a_row(monkeypatch):
    """Adopting would make the sweep's own row refuse the legitimate DevLoop's
    acquire until it aged out (ADR-010 pilot case 4 / #334)."""
    fake = _Fake({})
    result = await _run(fake, monkeypatch, active=[LIVE_WORKFLOW_ID])
    assert fake.writes == []
    assert result.escalations >= 1
    assert _finding(result, PR_ID).action == "escalate"


async def test_no_record_and_no_worker_is_not_escalated(monkeypatch):
    """The steady state must be silent.

    An entity nobody owns and nobody is working on is either a queued
    proposal before its implementer starts, or drift `detect_orphans` reports
    in this same tick. Escalating it here would fire on every actionable
    proposal every 15 minutes and make the counter useless for the condition
    it exists to catch.
    """
    fake = _Fake({})
    result = await _run(fake, monkeypatch, active=[])
    assert fake.writes == []
    assert result.escalations == 0
    assert {f.action for f in result.findings} == {"none"}


async def test_both_entity_phases_are_examined(monkeypatch):
    """The proposal's implement phase has a writer that can die too — the
    implementer — so a sweep that only looked at PRs would never see it."""
    fake = _Fake({PR_ID: _record()})
    result = await _run(fake, monkeypatch)
    assert {f.entity_id for f in result.findings} == {PR_ID, PROPOSAL_ID}
    assert _finding(result, PROPOSAL_ID).phase == "implement"


async def test_a_terminal_proposal_is_examined_without_reading_its_pr(monkeypatch):
    """A `merged` proposal answers both questions a snapshot would — which PR
    it owns is in `pr_url`, and that the entity is finished is the status — so
    fetching it is a `GET /pulls/{n}` per tick on the shared token, over the
    one bucket that only grows. The observation must survive the saving."""
    merged = [replace(_refs()[0], status="merged")]
    fake = _Fake({PR_ID: _record(healthy=False, dead=True, derived={"status": "dead", "held": True})})
    _wire(monkeypatch, fake, mode=rollout.ENFORCE, refs=merged)

    asked: list[list[ProposalStateRef]] = []

    async def _fetch(refs_arg):
        asked.append(list(refs_arg))
        return {}

    monkeypatch.setattr(act, "fetch_pr_snapshots", _fetch)
    result = await ActivityEnvironment().run(act.reconcile_lifecycle_ownership, [])

    assert asked == [[]]
    # Still both entities, and the PR is still seen as terminal — a dead owner
    # on it gets recovered and closed, not recovered and released.
    assert {f.entity_id for f in result.findings} == {PR_ID, PROPOSAL_ID}
    assert [p for p, _ in fake.writes] == [
        "/api/v1/lifecycle/ownership/recover",
        "/api/v1/lifecycle/ownership/terminal",
    ]


async def test_a_proposal_recovery_sends_no_entity_version(monkeypatch):
    """A proposal has no content hash to fence on. Sending the PR's head here
    would pin the row's `entity_version` to a value that names a different
    entity; blank is what makes mctl-api keep whatever the row already has
    (COALESCE/NULLIF in internal/lifecycle/store.go)."""
    fake = _Fake(
        {
            PROPOSAL_ID: _record(
                entity={"kind": "devloop-proposal", "id": PROPOSAL_ID, "version": ""},
                phase="implement",
                healthy=False,
                dead=True,
                derived={"status": "dead", "held": True},
            )
        }
    )
    result = await _run(fake, monkeypatch)
    recover = next(b for p, b in fake.writes if p.endswith("/recover"))
    assert recover["version"] == ""
    assert _finding(result, PROPOSAL_ID).action == "recover"


async def test_a_pr_read_that_404s_says_nothing_about_the_entity(monkeypatch):
    """`fetch_pr_snapshots` returns without a snapshot on a 404, deliberately:
    a PR we cannot see is absent, not finished. Reading that absence as
    terminality would close the row of an OPEN PR — and ADR-010 §5 has no
    arrow out of `terminal`, so the sweep would wedge the entity it exists to
    un-wedge. Unknown is not terminal, the same way unknown is not unowned."""
    fake = _Fake({PR_ID: _record(healthy=False, dead=True, derived={"status": "dead", "held": True})})
    result = await _run(fake, monkeypatch, snapshots={})
    assert [f.entity_id for f in result.findings] == [PROPOSAL_ID]
    assert fake.writes == []


async def test_a_review_stuck_pr_is_closed_not_released(monkeypatch):
    """`review-stuck` is in TERMINAL_STATUSES (ADR-010 §5) although its PR is
    normally still open — the entity's lifecycle has stopped until a human
    moves it. It is the one member of the set the shepherd can bring back, so
    its classification is pinned rather than left to the fetch filter."""
    stuck = [replace(_refs()[0], status="review-stuck")]
    fake = _Fake({PR_ID: _record(healthy=False, dead=True, derived={"status": "dead", "held": True})})
    result = await _run(fake, monkeypatch, refs=stuck, snapshots={})
    assert [p for p, _ in fake.writes] == [
        "/api/v1/lifecycle/ownership/recover",
        "/api/v1/lifecycle/ownership/terminal",
    ]
    assert "recovered and closed" in _finding(result, PR_ID).evidence
