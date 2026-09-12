"""The lifecycle activity's soft-fail contract.

Every path here must return an `unknown` verdict rather than raise. An activity
that hard-fails takes its retry budget and then the workflow step with it, and
this one is called from a loop that has already produced a PR — trading a
bookkeeping outage for a delivery outage is strictly worse than not knowing who
owns something.
"""
from __future__ import annotations

from typing import Any

import pytest

from orchestrator.temporal.activities import lifecycle as act


def _req(**kw: Any) -> act.OwnershipRequest:
    base = {
        "op": "acquire",
        "kind": "pull-request",
        "entity_id": "mctlhq/mctl-web#42",
        "phase": "review-remediation",
        "owner_type": "devloop-workflow",
        "owner_id": "dev-loop-mctlhq-mctl-web-7",
    }
    base.update(kw)
    return act.OwnershipRequest(**base)


def _record(**kw: Any) -> dict[str, Any]:
    rec = {
        "phase": "review-remediation",
        "owner": {"type": "devloop-workflow", "id": "dev-loop-mctlhq-mctl-web-7"},
        "epoch": 3,
        "state": "active",
        "healthy": True,
    }
    rec.update(kw)
    return rec


def test_unrecognised_200_is_unknown_not_a_confident_answer() -> None:
    """`from_payload` returns None for a body that is not a record, and the
    caller must not then read attributes off it — nor invent an empty record,
    which would answer OWNED_BY_OTHER with total confidence."""
    for payload in ({}, {"unexpected": "envelope"}, {"entity": "not-a-dict"}):
        result = act._result_from(payload, _req())
        assert result.verdict == act.UNKNOWN, payload
        assert result.owned_by_caller is False


def test_released_and_terminal_records_are_unowned() -> None:
    """A finished row still names an owner. Answering OWNED_BY_OTHER would make
    the next actor stand down on an entity that was explicitly handed back."""
    for state in ("released", "terminal"):
        result = act._result_from(_record(state=state, healthy=False), _req())
        assert result.verdict == act.UNOWNED, state


def test_my_own_healthy_record_is_owned_by_me() -> None:
    result = act._result_from(_record(), _req())
    assert result.verdict == act.OWNED_BY_ME
    assert result.owned_by_caller is True
    assert result.epoch == 3


def test_somebody_elses_record_is_owned_by_other() -> None:
    result = act._result_from(_record(owner={"type": "pr-steward", "id": "steward"}), _req())
    assert result.verdict == act.OWNED_BY_OTHER
    assert result.owned_by_caller is False


def test_unknown_op_is_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A typo in the op must not reach the network, and must not look like an
    ownership answer."""
    import anyio

    result = anyio.run(act.lifecycle_ownership, _req(op="acqiure"))
    assert result.verdict == act.UNKNOWN
    assert "acqiure" in result.reason


def test_result_defaults_are_conservative() -> None:
    """A result deserialized from a history recorded before a field existed, or
    built from a failed call, must default to the direction that does not act."""
    empty = act.OwnershipResult()
    assert empty.verdict == act.UNKNOWN
    assert empty.owned_by_caller is False
