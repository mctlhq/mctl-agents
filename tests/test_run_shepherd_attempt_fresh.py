"""`_attempt_is_fresh` as the union of claim and yaml lease (ADR-010 phase 2,
#352): held if an active claim exists OR the yaml lease is unexpired, and the
yaml branch now also requires a named holder instead of `expires_at` alone.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from orchestrator import run_shepherd
from orchestrator.lifecycle.contract import CLAIM_HELD_BY_ME, CLAIM_UNKNOWN, ClaimAnswer


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


def test_unexpired_lease_with_no_holder_is_not_fresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The defect this replaces: the holder id was written and never
    compared. A lease with no named holder must not be treated as held."""
    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, expires_at=future)
    assert run_shepherd._attempt_is_fresh(ref) is False


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


def test_at_only_the_yaml_lease_is_not_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """At `only` a claim is the sole answer: an unexpired yaml lease no
    longer matters once the claim answers negatively."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "only")
    monkeypatch.setattr(
        run_shepherd.ClaimClient, "check", lambda self, *a, **kw: ClaimAnswer(verdict=CLAIM_UNKNOWN)
    )
    future = (datetime.now(UTC) + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")
    ref = _ref(tmp_path, id="attempt-1", expires_at=future)
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
