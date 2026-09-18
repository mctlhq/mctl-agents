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
    CLAIM_UNKNOWN,
    ClaimAnswer,
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
