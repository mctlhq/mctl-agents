"""The reconcile decision table (#353), one test per condition it must handle.

These call `classify` directly. That is the point of it being pure: the
conditions #353 enumerates — orphaned, stale, conflicting, incomplete handoff
— are properties of a RECORD, and exercising them through HTTP would test the
transport instead, which is where the previous phases' tests already live.

The invariant every test here defends is one-directional: the sweep may refuse
to act on something broken, but it may never act on something that is not.
"""
from __future__ import annotations

from orchestrator.lifecycle import reconciler as r
from orchestrator.lifecycle.contract import (
    CLAIM_HELD_BY_OTHER,
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    OWNER_DEVLOOP_WORKFLOW,
    OWNER_RECONCILER,
    OWNER_SHEPHERD,
    UNKNOWN,
    UNOWNED,
    Owner,
)

ME = Owner(type=OWNER_RECONCILER, id="reconcile-2026-09-19")
DEVLOOP = Owner(type=OWNER_DEVLOOP_WORKFLOW, id="dev-loop-mctlhq-mctl-web-10")
SHEPHERD = Owner(type=OWNER_SHEPHERD, id="shepherd")


def _held(**kw) -> r.Observation:
    """A healthy, live, owned record — the baseline every case perturbs."""
    base = dict(
        kind="pull-request",
        entity_id="mctlhq/mctl-web#42",
        phase="review-remediation",
        verdict=OWNED_BY_OTHER,
        state="active",
        owner=DEVLOOP,
        epoch=3,
        healthy=True,
        derived_status="healthy",
        head="abc1234",
        needs_owner=True,
        temporal_workflow_id=DEVLOOP.id,
        live_workflow_id=DEVLOOP.id,
    )
    base.update(kw)
    return r.Observation(**base)


class TestNoMutationWithoutAnswer:
    def test_store_unknown_is_not_unowned(self):
        d = r.classify(_held(verdict=UNKNOWN, state="", owner=Owner()), ME)
        assert d.action == r.ACTION_SKIP
        assert d.reason == "store-unknown"
        assert not d.mutates

    def test_unrecognised_state_is_skipped_not_guessed(self):
        """An image a release behind mctl-api must not classify a state it
        cannot name — in either direction."""
        d = r.classify(_held(state="quiescing"), ME)
        assert d.action == r.ACTION_SKIP
        assert d.reason == "unrecognised-record"

    def test_unrecognised_state_is_skipped_even_when_dead(self):
        """Order matters: the dead branch is below the recognisability guard,
        so an unparseable record cannot be recovered on the strength of a flag
        that came with it."""
        d = r.classify(_held(state="quiescing", dead=True, healthy=False), ME)
        assert d.action == r.ACTION_SKIP
        assert not d.mutates

    def test_owned_by_me_is_not_classifiable(self):
        """The sweep asks as nobody, so OWNED_BY_ME can only mean the answer
        was built with a different `asking` — not a record to act on."""
        d = r.classify(_held(verdict=OWNED_BY_ME), ME)
        assert d.action == r.ACTION_SKIP
        assert d.reason == "unrecognised-record"


class TestZeroOwner:
    def test_live_work_without_a_record_escalates_and_writes_nothing(self):
        d = r.classify(_held(verdict=UNOWNED, state="", owner=Owner(), epoch=0), ME)
        assert d.action == r.ACTION_ESCALATE
        # A live worker against a record nobody wrote is the more alarming
        # half and gets its own slug, so an alert can separate them.
        assert d.reason == "zero-owner-live-worker"
        assert not d.mutates
        assert "abc1234" in d.evidence

    def test_zero_owner_without_a_worker(self):
        d = r.classify(
            _held(verdict=UNOWNED, state="", owner=Owner(), epoch=0, live_workflow_id=""),
            ME,
        )
        assert d.reason == "zero-owner"

    def test_unowned_and_terminal_is_settled(self):
        d = r.classify(
            _held(
                verdict=UNOWNED,
                state="released",
                owner=Owner(),
                entity_terminal=True,
                entity_terminal_reason="PR merged",
                needs_owner=False,
            ),
            ME,
        )
        assert d.action == r.ACTION_NONE
        assert d.reason == "terminal-unowned"

    def test_unowned_with_no_work_is_left_alone(self):
        d = r.classify(
            _held(verdict=UNOWNED, state="released", owner=Owner(), needs_owner=False),
            ME,
        )
        assert d.action == r.ACTION_NONE
        assert d.reason == "no-work"


class TestStaleOwner:
    def test_dead_owner_is_recovered_then_released(self):
        d = r.classify(_held(dead=True, healthy=False, derived_status="dead"), ME)
        assert d.action == r.ACTION_RECOVER
        assert d.reason == "dead-owner"
        assert d.to_owner == ME
        # The epoch READ, so a row that moved underneath this sweep fails 412
        # rather than being taken at whatever generation it now holds.
        assert d.expected_epoch == 3
        # The reconciler cannot advance a PR, so it must not keep one: a row
        # naming it would refuse the next real owner's acquire.
        assert d.then_release and not d.then_finalise

    def test_dead_owner_on_a_finished_entity_is_recovered_then_closed(self):
        d = r.classify(
            _held(
                dead=True,
                healthy=False,
                entity_terminal=True,
                entity_terminal_reason="PR merged",
                needs_owner=False,
            ),
            ME,
        )
        assert d.action == r.ACTION_RECOVER
        assert d.then_finalise and not d.then_release

    def test_stuck_owner_keeps_the_entity(self):
        """Losing PROGRESS is not losing liveness. Moving a stuck entity to
        another machine produces a second stuck machine (ADR-010 §4)."""
        d = r.classify(_held(stuck=True, healthy=False, derived_status="stuck"), ME)
        assert d.action == r.ACTION_ESCALATE
        assert d.reason == "stuck-owner"
        assert not d.mutates

    def test_dead_outranks_stuck(self):
        d = r.classify(_held(dead=True, stuck=True, healthy=False), ME)
        assert d.action == r.ACTION_RECOVER


class TestHandoff:
    def test_incomplete_handoff_is_completed_for_the_target(self):
        d = r.classify(_held(state="handing-off", handoff_to=SHEPHERD), ME)
        assert d.action == r.ACTION_COMPLETE_HANDOFF
        assert d.to_owner == SHEPHERD
        assert d.expected_epoch == 3

    def test_stalled_handoff_is_recovered_not_completed(self):
        """Both halves are gone: completing would hand the row to an actor
        that has already demonstrated it is not coming."""
        d = r.classify(
            _held(
                state="handing-off",
                handoff_to=SHEPHERD,
                dead=True,
                healthy=False,
                derived_status="handoff-stalled",
            ),
            ME,
        )
        assert d.action == r.ACTION_RECOVER
        assert d.reason == "handoff-stalled"
        assert d.to_owner == ME

    def test_handoff_without_a_target_escalates(self):
        d = r.classify(_held(state="handing-off", handoff_to=None), ME)
        assert d.action == r.ACTION_ESCALATE
        assert d.reason == "handoff-no-target"
        assert not d.mutates


class TestConflict:
    def test_a_second_live_worker_escalates(self):
        d = r.classify(_held(live_workflow_id="dev-loop-mctlhq-mctl-web-99"), ME)
        assert d.action == r.ACTION_ESCALATE
        assert d.reason == "conflicting-owner"
        assert "dev-loop-mctlhq-mctl-web-99" in d.evidence
        assert not d.mutates

    def test_a_stale_claim_is_reported_never_fenced(self):
        """The fences are the server's (ADR-010 §6). A client expiring another
        actor's claim would be asserting what rule 1 forbids."""
        d = r.classify(
            _held(claim_verdict=CLAIM_HELD_BY_OTHER, claim_state="active", claim_owner_epoch=2),
            ME,
        )
        assert d.action == r.ACTION_ESCALATE
        assert d.reason == "stale-claim"
        assert not d.mutates

    def test_claim_unknown_is_not_a_conflict(self):
        """Every claim read answers CLAIM_UNKNOWN until mctl-api ships the
        /lifecycle/claims/* routes. That must read as 'no information', not as
        'no claim' — otherwise the sweep's first enforce tick would escalate
        every healthy PR on the platform."""
        d = r.classify(_held(claim_owner_epoch=2, claim_entity_version="deadbee"), ME)
        assert d.action == r.ACTION_NONE
        assert d.reason == "healthy"


class TestHealthy:
    def test_a_live_owner_is_left_alone(self):
        d = r.classify(_held(), ME)
        assert d.action == r.ACTION_NONE
        assert d.reason == "healthy"
        assert not d.mutates

    def test_a_live_owner_closes_its_own_terminal_entity(self):
        d = r.classify(
            _held(entity_terminal=True, entity_terminal_reason="PR merged", needs_owner=False),
            ME,
        )
        assert d.action == r.ACTION_NONE
        assert d.reason == "terminal-live-owner"

    def test_every_action_is_in_the_closed_vocabulary(self):
        """No branch may invent a slug a caller's switch cannot enumerate."""
        for obs in (
            _held(),
            _held(dead=True, healthy=False),
            _held(stuck=True, healthy=False),
            _held(verdict=UNKNOWN),
            _held(verdict=UNOWNED, owner=Owner(), state=""),
            _held(state="handing-off", handoff_to=SHEPHERD),
            _held(state="handing-off", handoff_to=None),
        ):
            assert r.classify(obs, ME).action in r.ACTIONS
