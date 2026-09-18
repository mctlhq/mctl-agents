"""The reconcile sweep's I/O half (#353), driven over a faked transport.

The decision table is tested separately and purely. What is left here is
everything the pure module cannot see, and where every previous phase's
findings landed: the rollout gate, the batched read, and whether a decision
actually turns into the right sequence of writes at the right epoch.
"""
from __future__ import annotations

import json
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
    result = await _run(fake, monkeypatch)
    assert fake.writes == []
    assert result.escalations >= 1
    assert _finding(result, PR_ID).action == "escalate"


async def test_both_entity_phases_are_examined(monkeypatch):
    """The proposal's implement phase has a writer that can die too — the
    implementer — so a sweep that only looked at PRs would never see it."""
    fake = _Fake({PR_ID: _record()})
    result = await _run(fake, monkeypatch)
    assert {f.entity_id for f in result.findings} == {PR_ID, PROPOSAL_ID}
    assert _finding(result, PROPOSAL_ID).phase == "implement"
