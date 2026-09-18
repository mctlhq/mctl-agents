"""`_attempt_is_fresh` as the union of claim and yaml lease (ADR-010 phase 2,
#352): held if an active claim exists OR the yaml lease is unexpired. The
attempt block's `id` names the holder the claim branch asks about; the yaml
branch stays keyed on `expires_at`, because an unexpired lease is a live hold
whether or not a holder was recorded.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from orchestrator import run_shepherd
from orchestrator.lifecycle.contract import (
    CLAIM_HELD_BY_ME,
    CLAIM_HELD_BY_OTHER,
    CLAIM_UNCLAIMED,
    CLAIM_UNKNOWN,
    ClaimAnswer,
)


def _ref(tmp_path: Path, **attempt_fields) -> run_shepherd.ProposalRef:
    proposal_dir = tmp_path / "mctl-web" / "some-slug"
    proposal_dir.mkdir(parents=True)
    status = {"status": "in-progress"}
    if attempt_fields is not None:
        status["attempt"] = attempt_fields
    (proposal_dir / ".status.yaml").write_text(yaml.safe_dump(status), encoding="utf-8")
    return run_shepherd.ProposalRef(
        service="mctl-web", slug="some-slug", proposal_dir=proposal_dir, status="in-progress",
    )


def test_no_attempt_block_is_not_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    proposal_dir = tmp_path / "mctl-web" / "slug"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / ".status.yaml").write_text(yaml.safe_dump({"status": "in-progress"}), encoding="utf-8")
    ref = run_shepherd.ProposalRef(service="mctl-web", slug="slug", proposal_dir=proposal_dir, status="in-progress")
    assert run_shepherd._attempt_is_fresh(ref) is False


def test_unexpired_lease_with_a_holder_is_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=future)
    assert run_shepherd._attempt_is_fresh(ref) is True


def test_unexpired_lease_with_no_holder_is_still_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing holder id does not make a live 130-minute lease free.

    The `id` exists to be read — by the claim branch, which asks the claim
    about that named holder. Making it a precondition of the YAML branch
    instead would answer "not held" for every status file written before the
    id existed, and start a second implementer against a live one.
    """
    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, expires_at=future)
    assert run_shepherd._attempt_is_fresh(ref) is True


def test_expired_lease_is_not_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=past)
    assert run_shepherd._attempt_is_fresh(ref) is False


def test_below_enforce_a_claim_is_never_consulted(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`observe` computes and logs but composes no safety — the union begins
    at `enforce`, not before."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")

    def _boom(self, *a, **kw):
        raise AssertionError("a claim must not be consulted below enforce")

    monkeypatch.setattr(run_shepherd.ClaimClient, "check", _boom)
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=past)
    assert run_shepherd._attempt_is_fresh(ref) is False


def test_at_enforce_an_active_claim_makes_an_expired_yaml_lease_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    monkeypatch.setattr(
        run_shepherd.ClaimClient, "check", lambda self, *a, **kw: ClaimAnswer(verdict=CLAIM_HELD_BY_ME)
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=past)
    assert run_shepherd._attempt_is_fresh(ref) is True


def test_at_enforce_a_claim_held_by_another_is_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`may_execute` is False for both "unclaimed" and "held by other".

    Only the verdict tells them apart, and the difference is the whole point:
    treating an entity another executor actively holds as free races that
    holder at exactly the stage where the claim is the deciding mechanism.
    Untested on `ddcdb0e` — the suite covered CLAIM_HELD_BY_ME and
    CLAIM_UNKNOWN, i.e. both paths that do NOT exercise this branch.
    """
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "enforce")
    monkeypatch.setattr(
        run_shepherd.ClaimClient,
        "check",
        lambda self, *a, **kw: ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER),
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=past)
    assert run_shepherd._attempt_is_fresh(ref) is True


def test_at_only_a_claim_held_by_another_still_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The HELD_BY_OTHER arm returns before `new_answer_decides()` is asked,
    so it fails closed at `only` too, where the yaml lease is not consulted
    at all."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "only")
    monkeypatch.setattr(
        run_shepherd.ClaimClient,
        "check",
        lambda self, *a, **kw: ClaimAnswer(verdict=CLAIM_HELD_BY_OTHER),
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=past)
    assert run_shepherd._attempt_is_fresh(ref) is True


def test_at_only_the_yaml_lease_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At `only` a claim is the sole answer: an unexpired yaml lease no
    longer matters once the claim answers a DEFINITE no.

    Deliberately CLAIM_UNCLAIMED and not CLAIM_UNKNOWN — "nobody holds this"
    is an answer, "I could not reach the store" is not, and the two must not
    be demonstrated with the same fixture.
    """
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "only")
    monkeypatch.setattr(
        run_shepherd.ClaimClient, "check", lambda self, *a, **kw: ClaimAnswer(verdict=CLAIM_UNCLAIMED)
    )
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=future)
    assert run_shepherd._attempt_is_fresh(ref) is False


def test_an_unreachable_store_does_not_free_an_attempt_at_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Uncertainty never licenses a second executor.

    At `only` the yaml lease is not consulted, so before this arm an mctl-api
    outage answered "not fresh" for every in-flight attempt at once and the
    shepherd would have started a concurrent implementer against each — the
    store being down becoming the thing that breaks the invariant it enforces
    (agy P2 on `31232dc`).
    """
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "only")
    monkeypatch.delenv("LIFECYCLE_OWNERSHIP_REQUIRED", raising=False)
    monkeypatch.setattr(
        run_shepherd.ClaimClient, "check", lambda self, *a, **kw: ClaimAnswer(verdict=CLAIM_UNKNOWN)
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=past)
    assert run_shepherd._attempt_is_fresh(ref) is True


def test_the_break_glass_can_still_release_an_unknown_at_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`LIFECYCLE_OWNERSHIP_REQUIRED=false` is the documented break-glass, and
    it is the ONLY way an unreachable store stops holding attempts. Same gate
    `claim.blocks_mutation` uses, so the two cannot drift apart."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "only")
    monkeypatch.setenv("LIFECYCLE_OWNERSHIP_REQUIRED", "false")
    monkeypatch.setattr(
        run_shepherd.ClaimClient, "check", lambda self, *a, **kw: ClaimAnswer(verdict=CLAIM_UNKNOWN)
    )
    past = (datetime.now(UTC) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=past)
    assert run_shepherd._attempt_is_fresh(ref) is False


def test_merge_pr_ignores_claims_and_still_refuses_never_merge_services(monkeypatch, capsys) -> None:
    """A delegated claim contributes nothing to merge authority (#344):
    merge_pr does not read claims at all, and NEVER_MERGE_SERVICES is refused
    regardless of what any claim would say."""
    from unittest.mock import patch

    from tests.test_run_shepherd import make_pr

    pr = make_pr()
    pr.repo = "mctlhq/mctl-academy"
    with patch.object(run_shepherd, "subprocess") as mocked_subprocess:
        result = run_shepherd.merge_pr(pr)
    assert result == (False, None)
    mocked_subprocess.run.assert_not_called()
