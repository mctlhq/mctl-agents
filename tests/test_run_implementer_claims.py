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
from orchestrator.lifecycle.contract import CLAIM_FENCED, ClaimAnswer


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
