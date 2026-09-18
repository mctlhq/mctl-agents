"""ExecutionClaim wiring in the Tier 2 implementer (ADR-010 phase 2, #352).

Two properties matter most here: the push-site fence uses the explicit
`--force-with-lease=<branch>:<sha>` form and never the bare flag (T4), and the
implementer never mints a random attempt id (T7).
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from orchestrator import run_implementer
from orchestrator.lifecycle.contract import (
    CLAIM_FENCED,
    CLAIM_HELD_BY_ME,
    CLAIM_HELD_BY_OTHER,
    CLAIM_STATE_EXPIRED,
    CLAIM_STATE_FENCED,
    CLAIM_STATE_RELEASED,
    CLAIM_UNCLAIMED,
    CLAIM_UNKNOWN,
    ClaimAnswer,
    ExecutionClaim,
    claim_answer_from,
)


def test_push_followup_uses_the_explicit_force_with_lease_form(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def _fake_run(cmd, cwd=None, check=True, timeout=None):
        calls.append(cmd)
        return None

    monkeypatch.setattr(run_implementer, "_run", _fake_run)
    run_implementer._push_followup(Path("/tmp/repo"), "feat/agents-slug", "a" * 40, claim_context=None)

    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[:2] == ["git", "push"]
    lease_args = [a for a in cmd if a.startswith("--force-with-lease")]
    assert lease_args == [f"--force-with-lease=feat/agents-slug:{'a' * 40}"], cmd
    # Never the bare flag.
    assert "--force-with-lease" not in cmd


def test_push_followup_checks_the_claim_and_aborts_on_fence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **kw: calls.append(cmd))

    class _FencedClient:
        def check(self, *a, **kw):
            return ClaimAnswer(verdict=CLAIM_FENCED, reason="epoch moved")

    ctx = run_implementer._ClaimContext(
        client=_FencedClient(),
        claim_id="c1",
        entity=run_implementer.EntityRef.for_pull_request("mctlhq/mctl-web", 1, "a" * 40),
        phase=run_implementer.PHASE_REVIEW_REMEDIATION,
        owner_epoch=0,
        entity_version="a" * 40,
        executor=run_implementer.Executor(type=run_implementer.OWNER_IMPLEMENTER, id="attempt-1"),
        attempt="attempt-1",
    )
    with pytest.raises(run_implementer.ImplementerFenced):
        run_implementer._push_followup(Path("/tmp/repo"), "feat/agents-slug", "a" * 40, claim_context=ctx)
    # No git subprocess is invoked once the check fences.
    assert calls == []


def test_review_feedback_exit_code_maps_fenced_prefix() -> None:
    assert (
        run_implementer._review_feedback_exit_code(f"{run_implementer.FENCED_ERROR_PREFIX} claim fenced")
        == run_implementer.EXIT_FENCED
    )


def test_attempt_id_is_never_random_and_is_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    """T7. No uuid.uuid4() anywhere, and the same inputs always produce the
    same attempt id — the property a retried pod depends on to renew its own
    claim instead of being refused by it."""
    monkeypatch.delenv("WORKFLOW_UID", raising=False)
    first = run_implementer._resolve_attempt_id("mctl-web", "some-slug", 0, 0)
    second = run_implementer._resolve_attempt_id("mctl-web", "some-slug", 0, 0)
    assert first == second
    # Different inputs produce a different id.
    assert first != run_implementer._resolve_attempt_id("mctl-web", "other-slug", 0, 0)
    # Never a UUID: a UUID is 36 characters with dashes; sha256 hex is 64
    # lowercase hex characters with none.
    assert len(first) == 64
    assert "-" not in first


def test_workflow_uid_wins_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKFLOW_UID", "wf-123")
    assert run_implementer._resolve_attempt_id("mctl-web", "slug", 0, 0) == "wf-123"


def test_no_uuid_module_imported() -> None:
    """A regression guard on the defect itself: the module used to fall back
    to `str(uuid.uuid4())` for the attempt id. Removing the import entirely
    is stronger than scanning source for a call, since the docstrings here
    still name `uuid.uuid4()` to explain what not to do."""
    assert not hasattr(run_implementer, "uuid"), "uuid must not be imported at all"


def test_push_and_open_pr_adopts_an_existing_branch_with_a_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The adopt-existing-branch path CASes on the head it just observed,
    rather than blindly `-u` pushing again."""
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: "b" * 40)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **kw: calls.append(cmd))
    monkeypatch.setattr(run_implementer, "_open_pr_for_branch", lambda *_a, **_kw: "https://pr")

    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=Path("/tmp/proposal"), status="accepted",
    )
    with patch.object(run_implementer, "_open_pr_for_branch", return_value="https://pr"):
        result = run_implementer._push_and_open_pr(Path("/tmp/repo"), ref, claim_context=None)

    assert result == "https://pr"
    push_calls = [c for c in calls if c[:2] == ["git", "push"]]
    assert len(push_calls) == 1
    assert f"--force-with-lease=feat/agents-slug:{'b' * 40}" in push_calls[0]
    assert "-u" not in push_calls[0]


def test_push_and_open_pr_uses_dash_u_for_a_brand_new_branch(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: False)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **kw: calls.append(cmd))

    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=Path("/tmp/proposal"), status="accepted",
    )
    with patch.object(run_implementer, "_open_pr_for_branch", return_value="https://pr"):
        run_implementer._push_and_open_pr(Path("/tmp/repo"), ref, claim_context=None)

    assert calls == [["git", "push", "-u", "origin", "feat/agents-slug"]]


def test_attempt_id_distinguishes_two_hosts(monkeypatch: pytest.MonkeyPatch) -> None:
    """Determinism must not become a collision.

    Two pods on the same proposal in the same epoch used to derive the SAME
    id, so each one's `acquire` read as the other renewing its own claim —
    CLAIM_HELD_BY_ME to both, and the mechanism meant to stop concurrent
    implementers licensed them instead. `HOSTNAME` is the pod name, so it is
    stable across a container restart within a pod (determinism kept) and
    different across pods (collision gone).
    """
    monkeypatch.delenv("WORKFLOW_UID", raising=False)
    monkeypatch.setenv("HOSTNAME", "implementer-abc")
    first = run_implementer._resolve_attempt_id("mctl-web", "slug", 0, 0)
    assert first == run_implementer._resolve_attempt_id("mctl-web", "slug", 0, 0)
    monkeypatch.setenv("HOSTNAME", "implementer-def")
    assert first != run_implementer._resolve_attempt_id("mctl-web", "slug", 0, 0)


def _client_answering(answer):
    class _Client:
        def acquire(self, *a, **kw):
            return answer

        def check(self, *a, **kw):
            return answer

    return _Client


def _acquire(monkeypatch, answer):
    monkeypatch.setattr(run_implementer, "ClaimClient", _client_answering(answer))
    return run_implementer._acquire_claim(
        run_implementer.EntityRef.for_proposal("mctl-web", "slug"),
        run_implementer.PHASE_IMPLEMENT,
        "",
        "attempt-1",
        lease_seconds=60,
    )


def _retaking_client(renew_answer, *, claim_id: str = "c1"):
    """A store that refuses the acquire with a 409 naming THIS attempt — the
    orphan-claim retake — and answers `renew` with whatever the test wants."""
    acquired = ClaimAnswer(
        verdict=CLAIM_HELD_BY_ME,
        claim=ExecutionClaim(claim_id=claim_id, state="active"),
        reason="409 claim-held",
        retaken=True,
    )
    renews: list[dict] = []

    class _Client:
        calls = renews

        def acquire(self, *a, **kw):
            return acquired

        def renew(self, cid, *a, **kw):
            renews.append({"claim_id": cid, "lease_seconds": kw.get("lease_seconds")})
            return renew_answer

        def check(self, *a, **kw):
            return acquired

    return _Client


def test_a_retaken_claim_is_renewed_before_the_run_leans_on_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 409 never applied `lease_seconds`, so the adopted claim carries the
    DEAD pod's remaining lease while `implement_one` stamps a fresh full-length
    yaml one — the claim expiring before the run it guards, which is the
    direction that lets a second implementer start (claude P2 on `6794aad`)."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    client = _retaking_client(
        ClaimAnswer(verdict=CLAIM_HELD_BY_ME, claim=ExecutionClaim(claim_id="c1", state="active"))
    )
    monkeypatch.setattr(run_implementer, "ClaimClient", client)
    ctx = run_implementer._acquire_claim(
        run_implementer.EntityRef.for_proposal("mctl-web", "slug"),
        run_implementer.PHASE_IMPLEMENT,
        "",
        "attempt-1",
        lease_seconds=60,
    )
    assert ctx is not None
    assert ctx.claim_id == "c1"
    assert client.calls == [{"claim_id": "c1", "lease_seconds": 60}], client.calls


def test_a_granted_acquire_is_not_renewed_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """The renew belongs to the retake alone: a granted acquire already applied
    the lease, and a second write per attempt would be noise in the event log."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    client = _retaking_client(ClaimAnswer(verdict=CLAIM_HELD_BY_ME))

    class _Granted(client):  # type: ignore[misc, valid-type]
        def acquire(self, *a, **kw):
            return ClaimAnswer(
                verdict=CLAIM_HELD_BY_ME, claim=ExecutionClaim(claim_id="c1", state="active")
            )

    monkeypatch.setattr(run_implementer, "ClaimClient", _Granted)
    ctx = run_implementer._acquire_claim(
        run_implementer.EntityRef.for_proposal("mctl-web", "slug"),
        run_implementer.PHASE_IMPLEMENT,
        "",
        "attempt-1",
        lease_seconds=60,
    )
    assert ctx is not None
    assert client.calls == [], client.calls


def test_a_refused_renew_is_the_refusal_the_409_originally_was(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Adopting a claim this attempt cannot extend is worse than not adopting
    it: the run would proceed past a lease nobody is holding."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    monkeypatch.setattr(
        run_implementer, "ClaimClient",
        _retaking_client(ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER, reason="pod-2 took it")),
    )
    with pytest.raises(run_implementer.ImplementerClaimRefused):
        run_implementer._acquire_claim(
            run_implementer.EntityRef.for_proposal("mctl-web", "slug"),
            run_implementer.PHASE_IMPLEMENT,
            "",
            "attempt-1",
            lease_seconds=60,
        )


def test_a_fence_while_renewing_the_retaken_claim_fences_the_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    monkeypatch.setattr(
        run_implementer, "ClaimClient",
        _retaking_client(ClaimAnswer(verdict=CLAIM_FENCED, reason="epoch moved")),
    )
    with pytest.raises(run_implementer.ImplementerFenced):
        run_implementer._acquire_claim(
            run_implementer.EntityRef.for_proposal("mctl-web", "slug"),
            run_implementer.PHASE_IMPLEMENT,
            "",
            "attempt-1",
            lease_seconds=60,
        )


def test_a_refused_renew_is_advisory_below_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same rollout predicate as every other refusal here, so the stages cannot
    drift: below `enforce` the claim is declined, not raised on."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    monkeypatch.setattr(
        run_implementer, "ClaimClient",
        _retaking_client(ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER, reason="pod-2 took it")),
    )
    assert run_implementer._acquire_claim(
        run_implementer.EntityRef.for_proposal("mctl-web", "slug"),
        run_implementer.PHASE_IMPLEMENT,
        "",
        "attempt-1",
        lease_seconds=60,
    ) is None


def test_a_renew_the_store_performed_without_a_record_keeps_the_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retake made `renew` a safety input for the first time, and mctl-api
    may answer a renew it PERFORMED with a `204` or a bare `{"ok": true}`.
    Reading that as UNKNOWN refuses the restarted attempt on every restart
    until the orphan lease expires — the stall the retake exists to end
    (claude P2 on `0af3b38`). The claim id is the one the 409 named; nothing
    was left for the record to resolve."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    client = _retaking_client(
        claim_answer_from(
            204,
            {},
            run_implementer.Executor(type=run_implementer.OWNER_IMPLEMENTER, id="attempt-1"),
            path="/api/v1/lifecycle/claims/renew",
            body_empty=True,
        )
    )
    monkeypatch.setattr(run_implementer, "ClaimClient", client)
    ctx = run_implementer._acquire_claim(
        run_implementer.EntityRef.for_proposal("mctl-web", "slug"),
        run_implementer.PHASE_IMPLEMENT,
        "",
        "attempt-1",
        lease_seconds=60,
    )
    assert ctx is not None
    assert ctx.claim_id == "c1"


def test_acquire_returns_none_below_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """Below `enforce` a rejected acquire is advisory: the run proceeds on the
    pre-claim mechanisms exactly as it did before ADR-010 phase 2."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    assert _acquire(monkeypatch, ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER)) is None


def test_acquire_raises_when_another_executor_holds_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect on `ddcdb0e`: a rejected acquire returned None, and every
    downstream check is guarded by `if claim_context is not None` — so the
    rejection DISABLED the fencing instead of declining the attempt."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    with pytest.raises(run_implementer.ImplementerClaimRefused):
        _acquire(monkeypatch, ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER, reason="pod-2 holds it"))


def test_acquire_raises_on_a_fence_at_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    with pytest.raises(run_implementer.ImplementerFenced):
        _acquire(monkeypatch, ClaimAnswer(verdict=CLAIM_FENCED, reason="epoch moved"))


def test_a_fence_is_advisory_below_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """`observe` RECORDS writes, so a real 409 `fenced` does come back there.

    Halting on it would be the observe stage composing safety, which is
    precisely what that stage must not do (ADR-010 §12; agy P2 on `31232dc`).
    Gated by one predicate here and in `_check_claim_or_raise`, so the two
    sites cannot answer one verdict two different ways.
    """
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    assert _acquire(monkeypatch, ClaimAnswer(verdict=CLAIM_FENCED, reason="epoch moved")) is None


def test_a_fence_before_a_push_is_advisory_below_enforce(monkeypatch: pytest.MonkeyPatch) -> None:
    """The same predicate on the push-site check: observe logs the divergence
    and pushes, enforce raises."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **kw: calls.append(cmd))

    class _FencedClient:
        def check(self, *a, **kw):
            return ClaimAnswer(verdict=CLAIM_FENCED, reason="epoch moved")

    run_implementer._push_followup(
        Path("/tmp/repo"), "feat/agents-slug", "a" * 40, claim_context=_ctx(_FencedClient()),
    )
    assert len(calls) == 1, calls


def _ctx(client):
    return run_implementer._ClaimContext(
        client=client,
        claim_id="c1",
        entity=run_implementer.EntityRef.for_proposal("mctl-web", "slug"),
        phase=run_implementer.PHASE_IMPLEMENT,
        owner_epoch=0,
        entity_version="",
        executor=run_implementer.Executor(
            type=run_implementer.OWNER_IMPLEMENTER, id="attempt-1"
        ),
        attempt="attempt-1",
    )


def test_adopt_path_does_not_pin_the_proposal_claim_to_a_branch_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A proposal claim is acquired with `entity_version=""` — it has no git
    head. Asserting the branch SHA against it at check time claims a pin the
    claim never made, and the server fences on a version mismatch, aborting a
    perfectly good attempt. `--force-with-lease` stays the git CAS."""
    seen: list[tuple] = []

    class _Client:
        def check(self, *a, **kw):
            seen.append(a)
            return ClaimAnswer(verdict=CLAIM_HELD_BY_ME)

    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: "b" * 40)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **kw: None)
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=Path("/tmp/proposal"), status="accepted",
    )
    with patch.object(run_implementer, "_open_pr_for_branch", return_value="https://pr"):
        run_implementer._push_and_open_pr(Path("/tmp/repo"), ref, claim_context=_ctx(_Client()))

    assert seen, "the adopt path must still check the claim"
    # `check(claim_id, entity, phase, owner_epoch, entity_version, ...)` —
    # the version is positional arg 5, and must stay the empty one the claim
    # was acquired with, never the branch head.
    assert [args[4] for args in seen] == [""], seen


def test_brand_new_branch_push_is_also_claim_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """"No remote ref yet" is a statement about git, not about who may write.
    The model ran for a long time between the acquire and this push."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    calls: list[list[str]] = []

    class _FencedClient:
        def check(self, *a, **kw):
            return ClaimAnswer(verdict=CLAIM_FENCED, reason="epoch moved")

    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: False)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **kw: calls.append(cmd))
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=Path("/tmp/proposal"), status="accepted",
    )
    with patch.object(run_implementer, "_open_pr_for_branch", return_value="https://pr"):
        with pytest.raises(run_implementer.ImplementerFenced):
            run_implementer._push_and_open_pr(
                Path("/tmp/repo"), ref, claim_context=_ctx(_FencedClient()),
            )
    assert calls == []


@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        (lambda: (_ for _ in ()).throw(run_implementer.ImplementerOperationTimeout("slow")),
         "operation timed out"),
        (lambda: (_ for _ in ()).throw(RuntimeError("boom")), "unexpected error"),
    ],
)
def test_the_claim_is_released_on_a_failing_arm_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure, expected_reason: str,
) -> None:
    """The `finally` is what makes the release unconditional.

    A claim released on only the happy arm leaks on every other one, and a
    leaked claim holds the entity for the whole lease — up to 130 minutes of
    a live executor that no longer exists. Untested on `ddcdb0e`: nothing
    exercised an exception path with a claim held.
    """
    released: list[str] = []

    class _Client:
        def release(self, *a, reason: str = "", **kw):
            released.append(reason)

    proposal_dir = tmp_path / "mctl-web" / "slug"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / ".status.yaml").write_text(
        "status: in-review\npr: https://github.com/mctlhq/mctl-web/pull/7\n", encoding="utf-8",
    )
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=proposal_dir, status="in-review",
    )

    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: tmp_path / "repo")
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_checkout_existing_branch", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_capture_head_sha", lambda *_a, **_kw: "a" * 40)
    monkeypatch.setattr(run_implementer, "_acquire_claim", lambda *_a, **_kw: _ctx(_Client()))
    monkeypatch.setattr(run_implementer, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(run_implementer.anyio, "run", lambda *_a, **_kw: failure())

    result = run_implementer.review_feedback_one(ref, {"summaries": []})

    assert result.error
    assert released == [expected_reason], released


@pytest.mark.parametrize(
    ("verdict", "expect_released"),
    [(CLAIM_UNKNOWN, False), (CLAIM_HELD_BY_OTHER, True), (CLAIM_UNCLAIMED, True)],
)
def test_review_feedback_does_not_release_a_hold_it_could_not_confirm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, verdict: str, expect_released: bool,
) -> None:
    """The one exception to the unconditional `finally`. `implement_one`'s
    CLAIM_UNKNOWN arm states the rule — a release we cannot confirm is how an
    outage frees a live hold — and this function contradicted it by releasing
    on every arm including that one (claude P3 on `af661d7`). Every other
    refusal still releases: our claim_id names our own record, so the call
    frees nothing of a rival's."""
    released: list[str] = []

    class _Client:
        def release(self, *a, reason: str = "", **kw):
            released.append(reason)

    proposal_dir = tmp_path / "mctl-web" / "slug"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / ".status.yaml").write_text(
        "status: in-review\npr: https://github.com/mctlhq/mctl-web/pull/7\n", encoding="utf-8",
    )
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=proposal_dir, status="in-review",
    )

    def _refuse():
        raise run_implementer.ImplementerClaimRefused("claim-refused: x", verdict=verdict)

    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: tmp_path / "repo")
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_checkout_existing_branch", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_capture_head_sha", lambda *_a, **_kw: "a" * 40)
    monkeypatch.setattr(run_implementer, "_acquire_claim", lambda *_a, **_kw: _ctx(_Client()))
    monkeypatch.setattr(run_implementer, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(run_implementer.anyio, "run", lambda *_a, **_kw: _refuse())

    result = run_implementer.review_feedback_one(ref, {"summaries": []})

    assert result.error
    assert bool(released) is expect_released, released


def test_an_unreachable_store_is_not_reported_as_a_competing_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`ownership_required()` defaults to True, so at `enforce` a CLAIM_UNKNOWN
    is failed closed — correctly. What was wrong is what the operator then
    read: "another executor holds the claim", sending them to look for an
    executor that does not exist, when the real event is an expired token or a
    503 from mctl-api (claude P2 on `31232dc`)."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    monkeypatch.delenv("LIFECYCLE_OWNERSHIP_REQUIRED", raising=False)
    with pytest.raises(run_implementer.ImplementerClaimRefused) as excinfo:
        _acquire(monkeypatch, ClaimAnswer(verdict=CLAIM_UNKNOWN, reason="MCTL_TOKEN is not set"))
    message = str(excinfo.value)
    assert "another executor holds" not in message, message
    assert "could not answer" in message
    assert "MCTL_TOKEN is not set" in message
    assert "LIFECYCLE_OWNERSHIP_REQUIRED" in message


def test_the_break_glass_still_returns_none_on_an_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one path on which CLAIM_UNKNOWN really does return None — the
    documented break-glass, explicitly off. The docstring used to claim this
    was the DEFAULT path."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    monkeypatch.setenv("LIFECYCLE_OWNERSHIP_REQUIRED", "false")
    assert _acquire(monkeypatch, ClaimAnswer(verdict=CLAIM_UNKNOWN, reason="503")) is None


@pytest.mark.parametrize(
    ("verdict", "forbidden", "required"),
    [
        (CLAIM_UNKNOWN, "another executor holds", "could not answer"),
        (CLAIM_HELD_BY_OTHER, "could not answer", "another executor holds"),
    ],
)
def test_the_push_site_refusal_reads_the_same_as_the_acquire_one(
    monkeypatch: pytest.MonkeyPatch, verdict: str, forbidden: str, required: str,
) -> None:
    """One verdict, one wording, whichever site observed it — and the same
    sentinel exit code, so the shepherd cannot classify the two apart."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    monkeypatch.delenv("LIFECYCLE_OWNERSHIP_REQUIRED", raising=False)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **kw: None)

    class _Client:
        def check(self, *a, **kw):
            return ClaimAnswer(verdict=verdict, reason="store said so")

    with pytest.raises(run_implementer.ImplementerClaimRefused) as excinfo:
        run_implementer._push_followup(
            Path("/tmp/repo"), "feat/agents-slug", "a" * 40, claim_context=_ctx(_Client()),
        )
    message = str(excinfo.value)
    assert forbidden not in message, message
    assert required in message
    assert (
        run_implementer._review_feedback_exit_code(message) == run_implementer.EXIT_CLAIM_REFUSED
    )


def test_review_feedback_exit_code_maps_the_claim_refusal_prefix() -> None:
    """Left as EXIT_GENERIC_FAILURE, a refusal reached the operator as
    "follow-up subprocess failed transiently" — the exact legibility gap
    EXIT_FENCED was added to close — and repeated every tick for the life of a
    leaked lease (claude P3 on `31232dc`)."""
    assert (
        run_implementer._review_feedback_exit_code(
            f"{run_implementer.CLAIM_REFUSED_ERROR_PREFIX} another executor holds it"
        )
        == run_implementer.EXIT_CLAIM_REFUSED
    )
    assert run_implementer.EXIT_CLAIM_REFUSED != run_implementer.EXIT_FENCED


def test_an_unreadable_remote_head_falls_through_to_the_plain_push(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`--force-with-lease=<branch>:` means "the ref must not already exist" —
    the negation of the precondition that selected the existing-branch path.
    An unreadable head must not be expressed as a lease at all."""
    calls: list[list[str]] = []
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_remote_head_sha", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_run", lambda cmd, **kw: calls.append(cmd))
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=Path("/tmp/proposal"), status="accepted",
    )
    with patch.object(run_implementer, "_open_pr_for_branch", return_value="https://pr"):
        run_implementer._push_and_open_pr(Path("/tmp/repo"), ref, claim_context=None)

    assert calls == [["git", "push", "-u", "origin", "feat/agents-slug"]]


def test_a_refused_acquire_leaves_the_proposal_accepted_and_uncharged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The safety property the acquire/status reorder exists to produce, and
    the one a future refactor would silently undo by moving the status write
    back above the acquire: a lost race must write NOTHING — no `in-progress`
    flip, no 130-minute yaml lease stamped with our identity over the real
    holder's (claude P3 on `31232dc`)."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    proposal_dir = tmp_path / "mctl-web" / "slug"
    proposal_dir.mkdir(parents=True)
    status_path = proposal_dir / ".status.yaml"
    status_path.write_text("status: accepted\n", encoding="utf-8")
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=proposal_dir, status="accepted",
        approval_ok=True,
    )

    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda *_a, **_kw: None)
    # The GitHub preflight runs before the acquire and shells out to `gh`.
    # Stub it: on a runner with a token it answers for real, and this test is
    # about the claim, not about what GitHub says.
    monkeypatch.setattr(
        run_implementer,
        "_preflight_existing_result",
        lambda *_a, **_kw: run_implementer.ExistingResult(action="none"),
    )
    monkeypatch.setattr(run_implementer, "ClaimClient", _client_answering(
        ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER, reason="pod-2 holds it")
    ))
    monkeypatch.setattr(
        run_implementer, "_clone_target",
        lambda *_a, **_kw: pytest.fail("a refused acquire must not clone or run the model"),
    )

    result = run_implementer.implement_one(ref)

    assert result.counts_toward_limit is False
    assert result.pr_url is None
    assert "another executor holds" in (result.skipped_reason or "")
    body = status_path.read_text(encoding="utf-8")
    assert "in-progress" not in body, body
    assert "attempt" not in body, body


def test_a_vanished_claim_is_not_reported_as_a_competing_holder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLAIM_UNCLAIMED is the THIRD verdict that reaches the refusal branch —
    `claim_verdict_for` answers it for `released`, `expired` and `fenced` —
    and the common shape is this attempt's own lease running out, since nothing
    renews a claim mid-run — `ClaimClient.renew` runs once, on the retake, before
    the run starts. Naming a rival there sends
    the operator looking for an executor that does not exist and hides the one
    thing they can act on (claude P2 on `d5e2a48`)."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    with pytest.raises(run_implementer.ImplementerClaimRefused) as excinfo:
        _acquire(monkeypatch, ClaimAnswer(verdict=CLAIM_UNCLAIMED, reason="lease expired"))
    msg = str(excinfo.value)
    assert "was refused although the claim reads free" in msg
    assert "another executor holds" not in msg
    assert excinfo.value.verdict == CLAIM_UNCLAIMED


@pytest.mark.parametrize(
    ("verdict", "expected", "forbidden"),
    [
        (CLAIM_UNCLAIMED, "Nobody else holds it", "another executor holds"),
        (CLAIM_HELD_BY_OTHER, "another executor holds", "lease ran out"),
        (CLAIM_UNKNOWN, "could not answer", "another executor holds"),
    ],
)
def test_the_push_site_reports_each_verdict_as_itself(
    monkeypatch: pytest.MonkeyPatch, verdict: str, expected: str, forbidden: str,
) -> None:
    """One verdict, one wording, at both raise sites: the push-site check must
    not collapse three different situations into "another executor holds"."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    ctx = run_implementer._ClaimContext(
        client=_client_answering(ClaimAnswer(verdict=verdict, reason="because"))(),
        claim_id="c1",
        entity=run_implementer.EntityRef.for_pull_request("mctlhq/mctl-web", 1, "a" * 40),
        phase=run_implementer.PHASE_REVIEW_REMEDIATION,
        owner_epoch=0,
        entity_version="a" * 40,
        executor=run_implementer.Executor(type=run_implementer.OWNER_IMPLEMENTER, id="attempt-1"),
        attempt="attempt-1",
    )
    with pytest.raises(run_implementer.ImplementerClaimRefused) as excinfo:
        run_implementer._check_claim_or_raise(ctx)
    msg = str(excinfo.value)
    assert expected in msg
    assert forbidden not in msg


def test_a_push_site_refusal_is_a_skip_not_needs_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The acquire succeeds, the run happens, and the claim is gone by the
    time of the push. Without a dedicated arm this fell through to
    `except Exception` and was recorded as `unexpected-error`/`agent` — a
    crash in the proposal's history for a race the attempt did not cause, and
    the exact outcome `ImplementerClaimRefused`'s docstring forbids
    (claude + agy P2 on `d5e2a48`)."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    proposal_dir = tmp_path / "mctl-web" / "slug"
    proposal_dir.mkdir(parents=True)
    status_path = proposal_dir / ".status.yaml"
    status_path.write_text("status: accepted\n", encoding="utf-8")
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=proposal_dir, status="accepted",
        approval_ok=True,
    )

    answers = iter([
        ClaimAnswer(verdict=CLAIM_HELD_BY_ME),                        # acquire
        ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER, reason="pod-2 took it"),  # push site
    ])

    class _Client:
        def acquire(self, *a, **kw):
            return next(answers)

        def check(self, *a, **kw):
            return next(answers)

        def release(self, *a, **kw):
            return None

    monkeypatch.setattr(run_implementer, "ClaimClient", _Client)
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        run_implementer,
        "_preflight_existing_result",
        lambda *_a, **_kw: run_implementer.ExistingResult(action="none"),
    )
    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: tmp_path / "repo")
    monkeypatch.setattr(run_implementer, "_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_build_prompt", lambda *_a, **_kw: "prompt")
    monkeypatch.setattr(run_implementer.anyio, "run", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_detect_chart_major_bumps", lambda *_a, **_kw: [])
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: False)
    triaged: list[str] = []
    monkeypatch.setattr(
        run_implementer, "_mark_needs_triage",
        lambda *a, **kw: triaged.append(kw.get("code", "")),
    )

    result = run_implementer.implement_one(ref)

    assert triaged == [], triaged
    assert result.error is None
    assert "another executor holds" in (result.skipped_reason or "")
    # A full model pass already ran, so the batch budget IS charged — unlike
    # the acquire-site arm, which exits before anything is spent. Leaving it
    # uncharged lets one blip per proposal re-run the model down the whole
    # accepted queue (claude P2 on `c29195c`).
    assert result.counts_toward_limit is True
    # The status file keeps the holder's view of the world: this attempt does
    # not rewrite it back to `accepted` over a live `in-progress`.
    assert "in-progress" in status_path.read_text(encoding="utf-8")


def test_the_review_lease_always_outlives_the_run_it_covers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Nothing renews a claim mid-run, so a lease shorter than the run's own
    timeouts guarantees a CLAIM_UNCLAIMED refusal at the push site. The floor
    holds for the default timeouts; a raised IMPLEMENTER_TIMEOUT_SECONDS must
    widen the lease rather than silently outgrow it."""
    assert run_implementer._review_claim_lease_default() >= run_implementer.REVIEW_CLAIM_LEASE_FLOOR
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 7200.0)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_COMMAND_TIMEOUT_SECONDS", 300.0)
    widened = run_implementer._review_claim_lease_default()
    assert widened.total_seconds() >= 7200.0


def _implement_one_refused_at_the_push(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, push_answer: ClaimAnswer,
    *, stamp_other_attempt: bool = False, allow_triage: bool = False,
):
    """Drive `implement_one` past a successful acquire to a push-site refusal.

    With `stamp_other_attempt`, a second executor takes the proposal while
    this attempt is running: the SDK step rewrites `.status.yaml` with its own
    `attempt` block, which is the window every status write from an ending
    attempt has to survive.
    """
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    proposal_dir = tmp_path / "mctl-web" / "slug"
    proposal_dir.mkdir(parents=True)
    status_path = proposal_dir / ".status.yaml"
    status_path.write_text("status: accepted\n", encoding="utf-8")
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=proposal_dir, status="accepted",
        approval_ok=True,
    )
    answers = iter([ClaimAnswer(verdict=CLAIM_HELD_BY_ME), push_answer])
    released: list[str] = []

    class _Client:
        def acquire(self, *a, **kw):
            return next(answers)

        def check(self, *a, **kw):
            return next(answers)

        def release(self, *a, **kw):
            released.append(kw.get("reason", ""))
            return ClaimAnswer(verdict=CLAIM_UNCLAIMED)

    monkeypatch.setattr(run_implementer, "ClaimClient", _Client)
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        run_implementer, "_preflight_existing_result",
        lambda *_a, **_kw: run_implementer.ExistingResult(action="none"),
    )
    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a, **_kw: tmp_path / "repo")
    monkeypatch.setattr(run_implementer, "_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_build_prompt", lambda *_a, **_kw: "prompt")
    def _sdk(*_a, **_kw):
        if stamp_other_attempt:
            status_path.write_text(
                "status: in-progress\nattempt:\n  id: someone-else\n", encoding="utf-8"
            )

    monkeypatch.setattr(run_implementer.anyio, "run", _sdk)
    monkeypatch.setattr(run_implementer, "_has_new_commits", lambda *_a, **_kw: True)
    monkeypatch.setattr(run_implementer, "_detect_chart_major_bumps", lambda *_a, **_kw: [])
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a, **_kw: False)
    if not allow_triage:
        monkeypatch.setattr(
            run_implementer, "_mark_needs_triage",
            lambda *a, **kw: pytest.fail("a claim refusal must never mark needs-triage"),
        )

    result = run_implementer.implement_one(ref)
    return result, status_path.read_text(encoding="utf-8"), released


@pytest.mark.parametrize("state", [CLAIM_STATE_EXPIRED, CLAIM_STATE_RELEASED])
def test_a_vanished_claim_hands_the_proposal_back_for_an_immediate_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, state: str,
) -> None:
    """`expired` or `released` means NOBODY holds it — typically this attempt's
    own lease running out. Parking the proposal `in-progress` would then cost
    the rest of a 130-minute lease for a hold that does not exist, so the arm
    releases and restores `accepted` (claude P3 on `c29195c`)."""
    result, body, released = _implement_one_refused_at_the_push(
        tmp_path, monkeypatch,
        ClaimAnswer(
            verdict=CLAIM_UNCLAIMED,
            reason="lease expired",
            claim=ExecutionClaim(claim_id="c1", state=state),
        ),
    )
    assert result.error is None
    assert "in-progress" not in body, body
    assert "status: accepted" in body, body
    assert released, "a claim nobody holds must not be left dangling"


def test_a_fenced_record_is_not_treated_as_a_free_entity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`FREE_CLAIM_STATES` folds `fenced` in beside `released` and `expired`,
    so a claim fenced server-side by a NEWER executor comes back through
    `check` (which sends our own claim_id) as our own fenced record and reads
    CLAIM_UNCLAIMED. That executor is running right now and owns the `attempt`
    block; handing the proposal back would erase it and let a third
    implementer start (claude P2 on `af661d7`)."""
    result, body, _released = _implement_one_refused_at_the_push(
        tmp_path, monkeypatch,
        ClaimAnswer(
            verdict=CLAIM_UNCLAIMED,
            reason="fenced by a newer executor",
            claim=ExecutionClaim(claim_id="c1", state=CLAIM_STATE_FENCED),
        ),
    )
    assert result.error is None
    assert "in-progress" in body, body
    assert "status: accepted" not in body, body


def test_an_unclaimed_answer_with_no_record_proves_nothing_and_writes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The same rule for the answer that named no claim at all: a verdict
    without a record cannot license a write over somebody else's status."""
    result, body, _released = _implement_one_refused_at_the_push(
        tmp_path, monkeypatch, ClaimAnswer(verdict=CLAIM_UNCLAIMED, reason="no record"),
    )
    assert result.error is None
    assert "in-progress" in body, body


def test_a_409_fence_never_clobbers_the_new_holders_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The other encoding of the same fence. `claim_answer_from` turns a 2xx
    `{"state": "fenced"}` into CLAIM_UNCLAIMED but a 409 `{"code": "fenced"}`
    — ADR-010 §6's primary encoding — into CLAIM_FENCED, which lands in
    `except ImplementerFenced` and writes `needs-triage` carrying THIS
    attempt's block. Same hole as the hand-back: it erases the block
    `_attempt_is_fresh` reads, and parks the proposal at a human gate for a
    race the exception's own docstring calls not a failure of the proposal
    (claude P2 on `0808376`)."""
    result, body, released = _implement_one_refused_at_the_push(
        tmp_path, monkeypatch,
        ClaimAnswer(verdict=CLAIM_FENCED, reason="owner epoch moved"),
        stamp_other_attempt=True,
        allow_triage=True,
    )
    assert result.error
    assert "needs-triage" not in body, body
    assert "someone-else" in body, body
    assert released, "our own claim record is still let go of"
    assert "not recorded" in result.error, result.error


def test_a_fence_with_no_rival_still_records_needs_triage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The compare-and-swap narrows the write, it does not remove it: where
    `.status.yaml` still names this attempt, a fence is recorded exactly as
    before."""
    result, body, _released = _implement_one_refused_at_the_push(
        tmp_path, monkeypatch,
        ClaimAnswer(verdict=CLAIM_FENCED, reason="owner epoch moved"),
        allow_triage=True,
    )
    assert result.error
    assert "needs-triage" in body, body
    assert "code: fenced" in body, body
    assert "not recorded" not in result.error, result.error


def test_a_declined_hand_back_says_so_in_the_skip_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Handed back and NOT handed back are different outcomes — the first
    retries on the next tick, the second does not — so they must not read
    identically in the batch summary (claude P3 on `0808376`)."""
    result, _body, _released = _implement_one_refused_at_the_push(
        tmp_path, monkeypatch,
        ClaimAnswer(
            verdict=CLAIM_UNCLAIMED,
            reason="lease expired",
            claim=ExecutionClaim(claim_id="c1", state=CLAIM_STATE_EXPIRED),
        ),
        stamp_other_attempt=True,
    )
    assert result.skipped_reason
    assert "in-progress" in result.skipped_reason, result.skipped_reason


def test_the_hand_back_never_clobbers_a_second_executors_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The hand-back is a compare-and-swap. Where `.status.yaml` names a
    different attempt, the second executor took the proposal legitimately and
    its `attempt` block is the one the shepherd's `_attempt_is_fresh` reads —
    erasing it is how a third implementer starts (claude P2 on `af661d7`)."""
    proposal_dir = tmp_path / "mctl-web" / "slug"
    proposal_dir.mkdir(parents=True)
    status_path = proposal_dir / ".status.yaml"
    status_path.write_text(
        "status: in-progress\nattempt:\n  id: someone-else\n", encoding="utf-8"
    )
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="slug", proposal_dir=proposal_dir, status="in-progress",
        approval_ok=True,
    )

    assert run_implementer._hand_back_if_still_ours(ref, "ours") is False
    body = status_path.read_text(encoding="utf-8")
    assert "someone-else" in body, body
    assert "status: in-progress" in body, body

    assert run_implementer._hand_back_if_still_ours(ref, "someone-else") is True
    assert "status: accepted" in status_path.read_text(encoding="utf-8")


def test_an_unreachable_store_at_the_push_fails_closed_and_keeps_the_hold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opposite direction, and the reason the three verdicts cannot share
    one handler: on CLAIM_UNKNOWN nothing is known, so the yaml lease is left
    to expire on its own and the claim is deliberately NOT released — a
    release we cannot confirm is how an outage frees a live hold."""
    result, body, released = _implement_one_refused_at_the_push(
        tmp_path, monkeypatch, ClaimAnswer(verdict=CLAIM_UNKNOWN, reason="mctl-api unreachable"),
    )
    assert result.error is None
    assert "in-progress" in body, body
    assert released == [], released
