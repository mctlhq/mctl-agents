"""Tests for orchestrator.pr_adoption (mctlhq/mctl-agents#334).

Covers the flag surface (task 2), the `.prref.yaml` record (task 3), the
ownership/safety gates (task 4), the discovery pipeline (task 5), and the
proposal-less target support added to `run_implementer.py` (task 7/8).

GitHub API + the implementer subprocess are mocked at the module boundary,
same convention as tests/test_run_shepherd.py. `.prref.yaml` round-trips
through a real `tmp_path` worktree fixture.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from orchestrator import pr_adoption, run_implementer, run_shepherd
from orchestrator.lifecycle.contract import (
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    UNKNOWN,
    UNOWNED,
    Ownership,
    OwnershipAnswer,
)
from orchestrator.lifecycle.shadow import LEGACY_FREE, LEGACY_OWNED, LEGACY_UNKNOWN

HEAD_SHA = "a" * 40
HEAD_PUSHED_AT = "2026-04-29T10:00:00Z"


def _node(
    number: int,
    *,
    head_sha: str = HEAD_SHA,
    head_branch: str = "chore/manual-fix",
    is_draft: bool = False,
    is_cross_repository: bool = False,
    head_owner: str = "mctlhq",
    base_owner: str = "mctlhq",
) -> dict:
    return {
        "number": number,
        "headRefOid": head_sha,
        "headRefName": head_branch,
        "isDraft": is_draft,
        "isCrossRepository": is_cross_repository,
        "headRepositoryOwner": {"login": head_owner},
        "baseRepository": {"owner": {"login": base_owner}},
    }


def _graphql_view(nodes: list[dict]) -> dict:
    return {"data": {"repository": {"pullRequests": {"nodes": nodes}}}}


def make_pr(**kwargs) -> run_shepherd.PRSnapshot:
    defaults = dict(
        number=1,
        repo="mctlhq/mctl-web",
        state="OPEN",
        merged=False,
        closed_unmerged=False,
        merge_commit=None,
        close_comment_or_default="",
        head_sha=HEAD_SHA,
        head_pushed_at=HEAD_PUSHED_AT,
        merge_state_status="CLEAN",
        checks_green=True,
        is_draft=False,
    )
    defaults.update(kwargs)
    return run_shepherd.PRSnapshot(**defaults)


def make_finding(**kwargs) -> run_shepherd.CodexFinding:
    defaults = dict(
        body="![P1 Badge] fix this",
        path="a.py",
        line=1,
        commit_id=HEAD_SHA,
        created_at="2026-04-29T11:00:00Z",
        severity="P1",
        author=run_shepherd.REVIEW_BOT,
    )
    defaults.update(kwargs)
    return run_shepherd.CodexFinding(**defaults)


class _FakeOwnershipClient:
    """Injected in place of ``pr_adoption.OwnershipClient``."""

    def __init__(
        self,
        get_answer: OwnershipAnswer | None = None,
        acquire_answer: OwnershipAnswer | None = None,
        terminal_answer: OwnershipAnswer | None = None,
    ):
        self._get_answer = get_answer or OwnershipAnswer(verdict=UNOWNED)
        self._acquire_answer = acquire_answer or OwnershipAnswer(verdict=OWNED_BY_ME, accepted=True)
        self._terminal_answer = terminal_answer or OwnershipAnswer(verdict=UNOWNED, accepted=True)
        self.acquire_calls: list = []
        self.terminal_calls: list = []

    def get(self, entity, phase, asking=None):
        return self._get_answer

    def acquire(self, entity, phase, owner, **kwargs):
        self.acquire_calls.append((entity, phase, owner, kwargs))
        return self._acquire_answer

    def terminal(self, entity, phase, owner, epoch, reason=""):
        self.terminal_calls.append((entity, phase, owner, epoch, reason))
        return self._terminal_answer


def _client_factory(client: _FakeOwnershipClient):
    return lambda *a, **kw: client


# ---------------------------------------------------------------------------
# Flag surface (task 2)
# ---------------------------------------------------------------------------
def test_adoption_enabled_default_false(monkeypatch) -> None:
    monkeypatch.delenv("SHEPHERD_ADOPT_PRS", raising=False)
    assert pr_adoption.adoption_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_adoption_enabled_truthy_values(monkeypatch, value) -> None:
    monkeypatch.setenv("SHEPHERD_ADOPT_PRS", value)
    assert pr_adoption.adoption_enabled() is True


def test_adopt_repos_default_empty(monkeypatch) -> None:
    monkeypatch.delenv("SHEPHERD_ADOPT_REPOS", raising=False)
    assert pr_adoption.adopt_repos() == frozenset()


def test_adopt_repos_warns_on_unknown(monkeypatch, capsys) -> None:
    monkeypatch.setenv("SHEPHERD_ADOPT_REPOS", "mctl-web, not-a-real-service")
    repos = pr_adoption.adopt_repos()
    assert "mctl-web" in repos
    assert "not-a-real-service" in repos  # not dropped by _service_set_from_env itself
    out = capsys.readouterr().out
    assert "warn:" in out and "SHEPHERD_ADOPT_REPOS" in out


def test_max_prs_per_tick_default_and_override(monkeypatch) -> None:
    monkeypatch.delenv("SHEPHERD_ADOPT_MAX_PRS_PER_TICK", raising=False)
    assert pr_adoption.max_prs_per_tick() == 1
    monkeypatch.setenv("SHEPHERD_ADOPT_MAX_PRS_PER_TICK", "3")
    assert pr_adoption.max_prs_per_tick() == 3


def test_max_prs_per_tick_bad_value_warns_and_defaults(monkeypatch, capsys) -> None:
    monkeypatch.setenv("SHEPHERD_ADOPT_MAX_PRS_PER_TICK", "not-a-number")
    assert pr_adoption.max_prs_per_tick() == 1
    assert "warn:" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# The record (task 3)
# ---------------------------------------------------------------------------
def test_prref_mode_is_always_fix_only_regardless_of_caller(tmp_path) -> None:
    ref = pr_adoption.PRRef(
        service="mctl-web", slug="pr-1", proposal_dir=tmp_path / "pr-1",
        status="adopted", mode=run_shepherd.FULL,
    )
    assert ref.mode == run_shepherd.FIX_ONLY
    assert ref.is_adopted is True
    assert ref.status_path == tmp_path / "pr-1" / ".prref.yaml"


def test_write_prref_round_trip_matches_schema(tmp_path) -> None:
    path = pr_adoption.record_dir(tmp_path, "mctl-web", 42) / pr_adoption.PRREF_FILENAME
    pr_adoption.write_prref(
        path, "adopted",
        kind=pr_adoption.PRREF_KIND, repo="mctlhq/mctl-web", number=42,
        pr="https://github.com/mctlhq/mctl-web/pull/42",
        head_sha=HEAD_SHA, head_branch="chore/manual", owner_type="shepherd",
        policy_ref="service-mode:mctl-web=full",
        review_attempts=0, harness_failures=0, refusals=0, refusals_head=None,
        adopted_at="2026-01-01T00:00:00Z",
    )
    data = pr_adoption.load_prref(path)
    assert data["kind"] == "pr-ref"
    assert data["repo"] == "mctlhq/mctl-web"
    assert data["number"] == 42
    assert data["head_sha"] == HEAD_SHA
    assert data["head_branch"] == "chore/manual"
    assert data["owner_type"] == "shepherd"
    assert data["status"] == "adopted"
    assert data["review_attempts"] == 0


def test_write_prref_is_a_no_op_when_nothing_changed(tmp_path) -> None:
    path = tmp_path / ".prref.yaml"
    pr_adoption.write_prref(path, "adopted", repo="mctlhq/mctl-web", number=1)
    before = path.read_text(encoding="utf-8")
    pr_adoption.write_prref(path, "adopted", repo="mctlhq/mctl-web", number=1)
    after = path.read_text(encoding="utf-8")
    assert before == after


def test_write_prref_writes_when_a_field_changes(tmp_path) -> None:
    path = tmp_path / ".prref.yaml"
    pr_adoption.write_prref(path, "adopted", review_attempts=0)
    before = path.read_text(encoding="utf-8")
    pr_adoption.write_prref(path, "adopted", review_attempts=1)
    after = path.read_text(encoding="utf-8")
    assert before != after
    assert pr_adoption.load_prref(path)["review_attempts"] == 1


def test_append_evidence_caps_and_truncates(tmp_path) -> None:
    path = tmp_path / ".prref.yaml"
    long_finding = "x" * 1000
    for i in range(pr_adoption.MAX_EVIDENCE + 5):
        pr_adoption.append_evidence(
            path, at=f"t{i}", repo="mctlhq/mctl-web", pr=1, head_sha=HEAD_SHA,
            reviewer="claude[bot]", finding=long_finding, attempt=i,
            owner_type="shepherd", outcome="adopted",
        )
    data = pr_adoption.load_prref(path)
    evidence = data["evidence"]
    assert len(evidence) == pr_adoption.MAX_EVIDENCE
    # Oldest entries are dropped, newest kept.
    assert evidence[-1]["attempt"] == pr_adoption.MAX_EVIDENCE + 4
    assert len(evidence[0]["finding"]) == pr_adoption.MAX_FINDING_CHARS


# ---------------------------------------------------------------------------
# Ownership and safety gates (task 4 / T2 / T3)
# ---------------------------------------------------------------------------
def test_is_fork_raw_true_on_explicit_flag() -> None:
    node = _node(1, is_cross_repository=True)
    assert pr_adoption._is_fork_raw(node, "mctlhq") is True


def test_is_fork_raw_true_on_owner_mismatch_even_if_flag_false() -> None:
    node = _node(1, is_cross_repository=False, head_owner="someone-else")
    assert pr_adoption._is_fork_raw(node, "mctlhq") is True


def test_is_fork_raw_false_for_same_repo() -> None:
    node = _node(1)
    assert pr_adoption._is_fork_raw(node, "mctlhq") is False


def test_is_agents_branch() -> None:
    assert pr_adoption._is_agents_branch("feat/agents-issue-1-foo") is True
    assert pr_adoption._is_agents_branch("chore/manual-fix") is False


def test_devloop_free_fails_closed_on_unknown(monkeypatch) -> None:
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda *a, **kw: LEGACY_UNKNOWN)
    assert pr_adoption._devloop_free("mctl-web", "pr-1") is False


def test_devloop_free_refuses_on_owned(monkeypatch) -> None:
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda *a, **kw: LEGACY_OWNED)
    assert pr_adoption._devloop_free("mctl-web", "pr-1") is False


def test_devloop_free_true_on_free(monkeypatch) -> None:
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda *a, **kw: LEGACY_FREE)
    assert pr_adoption._devloop_free("mctl-web", "pr-1") is True


def test_devloop_free_checks_closing_issue_numbers(monkeypatch) -> None:
    """`slug_for(number)` ("pr-<n>") never matches `_dev_loop_owns_answer`'s
    `issue-(\\d+)-` probe, so the bare-slug check alone is structurally
    always LEGACY_FREE for an adopted PR (mctlhq/mctl-agents#334 code
    review). A closing-issue number naming an issue a DevLoopWorkflow owns
    must still refuse."""

    def fake_answer(service: str, slug: str) -> str:
        return LEGACY_OWNED if slug.startswith("issue-42-") else LEGACY_FREE

    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", fake_answer)
    assert pr_adoption._devloop_free("mctl-web", "pr-7", (42,)) is False


def test_devloop_free_true_when_closing_issues_all_free(monkeypatch) -> None:
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda *a, **kw: LEGACY_FREE)
    assert pr_adoption._devloop_free("mctl-web", "pr-7", (42, 43)) is True


def test_devloop_free_missing_token_warns_once(monkeypatch, capsys) -> None:
    """P2 (code review on issue-334): without MCTL_TOKEN,
    `_dev_loop_owns_answer` already short-circuits every closing-issue
    check to LEGACY_UNKNOWN with no network I/O — but silently, once per
    candidate. A shared `_DevLoopProbeBudget` must print the warning
    exactly once, however many candidates or closing issues are probed."""
    monkeypatch.delenv("MCTL_TOKEN", raising=False)

    def fake_answer(service: str, slug: str) -> str:
        # Mirrors the real function: a bare "pr-<n>" slug never matches the
        # issue-(\d+)- probe (structural LEGACY_FREE); a closing-issue slug
        # does match, and with no MCTL_TOKEN the real function's own "Never
        # asked." branch answers LEGACY_UNKNOWN.
        return LEGACY_UNKNOWN if slug.startswith("issue-") else LEGACY_FREE

    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", fake_answer)
    budget = pr_adoption._DevLoopProbeBudget()
    assert pr_adoption._devloop_free("mctl-web", "pr-7", (42,), probe_budget=budget) is False
    assert pr_adoption._devloop_free("mctl-web", "pr-8", (43,), probe_budget=budget) is False
    warnings = [line for line in capsys.readouterr().out.splitlines() if "MCTL_TOKEN is not set" in line]
    assert len(warnings) == 1


def test_devloop_free_probe_budget_caps_calls(monkeypatch) -> None:
    """P2 (code review on issue-334): closing-issue probes must be bounded
    per tick — up to 50 candidates x 5 closing issues each was unbounded,
    serial HTTP. Once the shared budget is spent, remaining candidates fail
    closed without calling `_dev_loop_owns_answer` for their closing
    issues."""
    monkeypatch.setenv("MCTL_TOKEN", "tok")
    calls: list[str] = []

    def fake_answer(service: str, slug: str) -> str:
        calls.append(slug)
        return LEGACY_FREE

    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", fake_answer)
    budget = pr_adoption._DevLoopProbeBudget(remaining=1)
    assert pr_adoption._devloop_free("mctl-web", "pr-7", (1,), probe_budget=budget) is True
    assert pr_adoption._devloop_free("mctl-web", "pr-8", (2,), probe_budget=budget) is False
    assert calls.count("issue-1-adopted-pr") == 1
    assert "issue-2-adopted-pr" not in calls


def test_closing_issue_numbers_extracts_from_node() -> None:
    node = {"closingIssuesReferences": {"nodes": [{"number": 42}, {"number": 43}, {}]}}
    assert pr_adoption._closing_issue_numbers(node) == (42, 43)


def test_closing_issue_numbers_defaults_empty() -> None:
    assert pr_adoption._closing_issue_numbers({}) == ()


def test_mode_permits_false_on_skip(monkeypatch) -> None:
    monkeypatch.setattr(run_shepherd, "_service_mode", lambda service, **kw: run_shepherd.SKIP)
    assert pr_adoption._mode_permits("mctl-gitops") is False


def test_mode_permits_true_otherwise(monkeypatch) -> None:
    monkeypatch.setattr(run_shepherd, "_service_mode", lambda service, **kw: run_shepherd.FULL)
    assert pr_adoption._mode_permits("mctl-web") is True


def test_store_permits_skipped_when_rollout_off(monkeypatch) -> None:
    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    entity = pr_adoption.EntityRef.for_pull_request("mctlhq/mctl-web", 1, HEAD_SHA)
    # Even an explosive client is never consulted at rollout `off`.
    with patch.object(pr_adoption, "OwnershipClient", side_effect=AssertionError("must not be called")):
        assert pr_adoption._store_permits(entity) is True


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [(UNOWNED, True), (OWNED_BY_ME, True), (OWNED_BY_OTHER, False), (UNKNOWN, False)],
)
def test_store_permits_admits_only_unowned_or_owned_by_me(monkeypatch, verdict, expected) -> None:
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    client = _FakeOwnershipClient(get_answer=OwnershipAnswer(verdict=verdict))
    monkeypatch.setattr(pr_adoption, "OwnershipClient", _client_factory(client))
    entity = pr_adoption.EntityRef.for_pull_request("mctlhq/mctl-web", 1, HEAD_SHA)
    assert pr_adoption._store_permits(entity) is expected


# ---------------------------------------------------------------------------
# Discovery pipeline (task 5) — T1 (adoption half), T2, T3, T9, T10
# ---------------------------------------------------------------------------
def _discover_env(monkeypatch, repos: str = "mctl-web") -> None:
    monkeypatch.setenv("SHEPHERD_ADOPT_REPOS", repos)
    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)  # store consultation off by default
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda *a, **kw: LEGACY_FREE)
    monkeypatch.setattr(run_shepherd, "_service_mode", lambda service, **kw: run_shepherd.FULL)


def test_discover_adoptable_adopts_a_candidate_with_fresh_p1(tmp_path, monkeypatch) -> None:
    """T1 (discovery half): a same-repo PR with a fresh P1 and no proposal is
    discovered and adopted; `.prref.yaml` is written with evidence."""
    _discover_env(monkeypatch)
    monkeypatch.setattr(
        pr_adoption, "_list_open_prs", lambda repo: [_node(7, head_branch="chore/manual")],
    )
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda repo, number: make_pr(number=number, repo=repo))
    monkeypatch.setattr(
        run_shepherd, "read_codex_review",
        lambda pr: run_shepherd.CodexReview(has_responded=True, findings=[make_finding()]),
    )

    refs = pr_adoption.discover_adoptable(tmp_path)

    assert len(refs) == 1
    ref = refs[0]
    assert isinstance(ref, pr_adoption.PRRef)
    assert ref.pr_url == "https://github.com/mctlhq/mctl-web/pull/7"
    assert ref.mode == run_shepherd.FIX_ONLY
    assert ref.status_path.exists()
    data = pr_adoption.load_prref(ref.status_path)
    assert data["kind"] == "pr-ref"
    assert data["status"] == "adopted"
    assert len(data["evidence"]) == 1
    ev = data["evidence"][0]
    assert set(ev) >= {"at", "repo", "pr", "head_sha", "reviewer", "finding", "attempt", "owner_type", "outcome"}
    assert ev["outcome"] == "adopted"


def test_discover_adoptable_no_fresh_finding_is_not_adopted(tmp_path, monkeypatch) -> None:
    """IF a candidate PR has no fresh P1/P2 finding THEN no adoption."""
    _discover_env(monkeypatch)
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda repo, number: make_pr(number=number, repo=repo))
    monkeypatch.setattr(
        run_shepherd, "read_codex_review",
        lambda pr: run_shepherd.CodexReview(has_responded=True, findings=[]),
    )
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []
    assert not (tmp_path / "mctl-web" / "adopted-prs").exists()


def test_discover_adoptable_stale_finding_predating_head_push_is_not_adopted(tmp_path, monkeypatch) -> None:
    """T4 (first half): a finding whose created_at predates head_pushed_at
    does not trigger adoption — `fresh_findings_p1_p2` is the only findings
    source, applied unchanged."""
    _discover_env(monkeypatch)
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda repo, number: make_pr(number=number, repo=repo))
    stale = make_finding(created_at="2020-01-01T00:00:00Z")  # predates HEAD_PUSHED_AT
    monkeypatch.setattr(
        run_shepherd, "read_codex_review",
        lambda pr: run_shepherd.CodexReview(has_responded=True, findings=[stale]),
    )
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_refuses_fork(tmp_path, monkeypatch, capsys) -> None:
    """T2: a fork PR is never adopted, and the findings gate (an expensive
    `gh api` fetch) is never reached for it."""
    _discover_env(monkeypatch)
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7, is_cross_repository=True)])
    monkeypatch.setattr(
        run_shepherd, "_fetch_pr_snapshot",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not fetch a fork PR's snapshot")),
    )
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []
    assert "not adoptable" in capsys.readouterr().out


def test_discover_adoptable_refuses_agents_branch(tmp_path, monkeypatch) -> None:
    _discover_env(monkeypatch)
    monkeypatch.setattr(
        pr_adoption, "_list_open_prs",
        lambda repo: [_node(7, head_branch="feat/agents-issue-9-foo")],
    )
    monkeypatch.setattr(
        run_shepherd, "_fetch_pr_snapshot",
        lambda *a, **kw: (_ for _ in ()).throw(AssertionError("must not fetch a deterministic-branch PR")),
    )
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_refuses_pr_already_owned_by_a_proposal(tmp_path, monkeypatch) -> None:
    """T3(a): a `.status.yaml` elsewhere carrying the same `pr:` URL."""
    _discover_env(monkeypatch)
    proposal_dir = tmp_path / "mctl-web" / "proposals" / "issue-1-foo"
    proposal_dir.mkdir(parents=True)
    (proposal_dir / ".status.yaml").write_text(
        yaml.safe_dump({"status": "implemented", "pr": "https://github.com/mctlhq/mctl-web/pull/7"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_refuses_devloop_owned(tmp_path, monkeypatch) -> None:
    """T3(c): _dev_loop_owns_answer returning LEGACY_OWNED."""
    _discover_env(monkeypatch)
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda *a, **kw: LEGACY_OWNED)
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_refuses_devloop_owned_via_closing_issue(tmp_path, monkeypatch) -> None:
    """`slug_for(number)` never matches `_dev_loop_owns_answer`'s regex, so
    this candidate would otherwise sail through the DevLoop gate regardless
    of a live DevLoopWorkflow on its GitHub-linked issue (mctlhq/mctl-agents#334
    code review). Wired through `_list_open_prs`' `closingIssuesReferences`."""
    _discover_env(monkeypatch)

    def fake_answer(service: str, slug: str) -> str:
        return LEGACY_OWNED if slug.startswith("issue-42-") else LEGACY_FREE

    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", fake_answer)
    node = _node(7)
    node["closingIssuesReferences"] = {"nodes": [{"number": 42}]}
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [node])
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_refuses_devloop_unknown(tmp_path, monkeypatch) -> None:
    """T3(d): LEGACY_UNKNOWN must also refuse — opposite of the sweep's
    fail-open default."""
    _discover_env(monkeypatch)
    monkeypatch.setattr(run_shepherd, "_dev_loop_owns_answer", lambda *a, **kw: LEGACY_UNKNOWN)
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_refuses_skip_mode(tmp_path, monkeypatch) -> None:
    """T3(e): _service_mode resolving SKIP."""
    _discover_env(monkeypatch)
    monkeypatch.setattr(run_shepherd, "_service_mode", lambda service, **kw: run_shepherd.SKIP)
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_refuses_store_owned_by_other(tmp_path, monkeypatch) -> None:
    """T3(f): the ownership store answering OWNED_BY_OTHER."""
    _discover_env(monkeypatch)
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    client = _FakeOwnershipClient(get_answer=OwnershipAnswer(verdict=OWNED_BY_OTHER))
    monkeypatch.setattr(pr_adoption, "OwnershipClient", _client_factory(client))
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_refuses_store_unknown(tmp_path, monkeypatch) -> None:
    """T3(g): the store answering UNKNOWN. Also proves an unreachable store
    yields zero adoptions and zero exceptions."""
    _discover_env(monkeypatch)
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    client = _FakeOwnershipClient(get_answer=OwnershipAnswer(verdict=UNKNOWN, reason="store down"))
    monkeypatch.setattr(pr_adoption, "OwnershipClient", _client_factory(client))
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_ownership_acquire_failure_downgrades_to_no_adopt(tmp_path, monkeypatch) -> None:
    """Ownership failure at the acquire step never fails the tick — it
    downgrades to 'do not adopt this PR', and writes nothing."""
    _discover_env(monkeypatch)
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    client = _FakeOwnershipClient(
        get_answer=OwnershipAnswer(verdict=UNOWNED),
        acquire_answer=OwnershipAnswer(verdict=UNKNOWN, reason="conflict"),
    )
    monkeypatch.setattr(pr_adoption, "OwnershipClient", _client_factory(client))
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [_node(7)])
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda repo, number: make_pr(number=number, repo=repo))
    monkeypatch.setattr(
        run_shepherd, "read_codex_review",
        lambda pr: run_shepherd.CodexReview(has_responded=True, findings=[make_finding()]),
    )
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []
    assert not (tmp_path / "mctl-web" / "adopted-prs").exists()
    assert client.acquire_calls  # the acquire really was attempted


def _terminal_ref(tmp_path, *, status: str = "merged") -> pr_adoption.PRRef:
    return pr_adoption.PRRef(
        service="mctl-web", slug="pr-7",
        proposal_dir=pr_adoption.record_dir(tmp_path, "mctl-web", 7),
        status=status, repo="mctlhq/mctl-web", number=7,
    )


def test_release_ownership_closes_the_row_when_owned_by_me(tmp_path, monkeypatch) -> None:
    """mctlhq/mctl-agents#334 code review: `_adopt`'s acquire has a matching
    close once the ref reaches a terminal status — the row is not left
    ACTIVE forever."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    client = _FakeOwnershipClient(
        get_answer=OwnershipAnswer(verdict=OWNED_BY_ME, ownership=Ownership(epoch=3, state="active")),
    )
    monkeypatch.setattr(pr_adoption, "OwnershipClient", _client_factory(client))
    ref = _terminal_ref(tmp_path, status="merged")

    pr_adoption.release_ownership(ref, reason="adopted PR reached terminal status 'merged'")

    assert len(client.terminal_calls) == 1
    entity, phase, owner, epoch, reason = client.terminal_calls[0]
    assert entity.id == "mctlhq/mctl-web#7"
    assert phase == pr_adoption.PHASE_REVIEW_REMEDIATION
    assert owner == pr_adoption._SHEPHERD_OWNER
    assert epoch == 3
    assert "merged" in reason


def test_release_ownership_noop_when_not_owned_by_me(tmp_path, monkeypatch) -> None:
    """A row already released, terminal, or held by somebody else is not
    this call's to close."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")
    client = _FakeOwnershipClient(get_answer=OwnershipAnswer(verdict=UNOWNED))
    monkeypatch.setattr(pr_adoption, "OwnershipClient", _client_factory(client))
    ref = _terminal_ref(tmp_path, status="rejected")

    pr_adoption.release_ownership(ref, reason="closed")

    assert client.terminal_calls == []


def test_release_ownership_skipped_when_rollout_off(tmp_path, monkeypatch) -> None:
    """Below `records_writes()` no row was ever written, so the store is
    never even consulted — mirrors `_store_permits`'s own rollout-off gate."""
    monkeypatch.delenv("LIFECYCLE_ROLLOUT_MODE", raising=False)
    ref = _terminal_ref(tmp_path, status="review-stuck")

    with patch.object(pr_adoption, "OwnershipClient", side_effect=AssertionError("must not be called")):
        pr_adoption.release_ownership(ref, reason="stuck")  # must not raise


def test_release_ownership_never_raises_on_transport_failure(tmp_path, monkeypatch, capsys) -> None:
    """Closing a row must never fail the status transition it follows."""
    monkeypatch.setenv("LIFECYCLE_ROLLOUT_MODE", "observe")

    class _Boom:
        def get(self, *a, **kw):
            raise RuntimeError("store unreachable")

    monkeypatch.setattr(pr_adoption, "OwnershipClient", lambda *a, **kw: _Boom())
    ref = _terminal_ref(tmp_path, status="merged")

    pr_adoption.release_ownership(ref, reason="merged")  # must not raise
    assert "warn:" in capsys.readouterr().out


def test_discover_adoptable_repo_listing_failure_yields_zero_and_no_exception(tmp_path, monkeypatch, capsys) -> None:
    _discover_env(monkeypatch)

    def _boom(repo):
        raise RuntimeError("gh api exploded")

    monkeypatch.setattr(pr_adoption, "_list_open_prs", _boom)
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []
    assert "warn:" in capsys.readouterr().out


def test_discover_adoptable_per_tick_cap(tmp_path, monkeypatch) -> None:
    """T10: three adoptable PRs, cap=1 -> exactly one adopted, others skipped
    (logged, not silently dropped)."""
    _discover_env(monkeypatch)
    nodes = [_node(n, head_branch=f"chore/fix-{n}") for n in (1, 2, 3)]
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: nodes)
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda repo, number: make_pr(number=number, repo=repo))
    monkeypatch.setattr(
        run_shepherd, "read_codex_review",
        lambda pr: run_shepherd.CodexReview(has_responded=True, findings=[make_finding()]),
    )
    refs = pr_adoption.discover_adoptable(tmp_path, budget=1)
    assert len(refs) == 1
    # Only one PR's directory was actually written.
    adopted_dirs = list((tmp_path / "mctl-web" / "adopted-prs").iterdir())
    assert len(adopted_dirs) == 1


def test_discover_adoptable_service_filter_scopes_to_one_service(tmp_path, monkeypatch) -> None:
    """P2 (code review on issue-334): `run_shepherd --service foo` must scope
    adoption the same way it already scopes proposal discovery
    (`_discover_refs`'s `service_filter`) — not sweep every allowlisted repo.
    """
    _discover_env(monkeypatch, repos="mctl-web,mctl-design")
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda repo, number: make_pr(number=number, repo=repo))
    monkeypatch.setattr(
        run_shepherd, "read_codex_review",
        lambda pr: run_shepherd.CodexReview(has_responded=True, findings=[make_finding()]),
    )
    listed: list[str] = []

    def _list(repo: str):
        listed.append(repo)
        return [_node(7, head_branch="chore/manual")]

    monkeypatch.setattr(pr_adoption, "_list_open_prs", _list)

    refs = pr_adoption.discover_adoptable(tmp_path, service_filter="mctl-design")

    assert listed == ["mctlhq/mctl-design"]
    assert len(refs) == 1
    assert refs[0].service == "mctl-design"


def test_discover_adoptable_budget_zero_or_no_repos_short_circuits(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("SHEPHERD_ADOPT_REPOS", "mctl-web")
    with patch.object(pr_adoption, "_list_open_prs", side_effect=AssertionError("must not query with budget=0")):
        assert pr_adoption.discover_adoptable(tmp_path, budget=0) == []
    monkeypatch.delenv("SHEPHERD_ADOPT_REPOS", raising=False)
    with patch.object(pr_adoption, "_list_open_prs", side_effect=AssertionError("must not query with no repos")):
        assert pr_adoption.discover_adoptable(tmp_path) == []


def test_discover_adoptable_reuses_existing_record_without_rewriting(tmp_path, monkeypatch) -> None:
    """WHEN a `.prref.yaml` already exists for a PR THE SYSTEM SHALL reuse it
    rather than adopt again, and leave the file byte-identical."""
    _discover_env(monkeypatch)
    path = pr_adoption.record_dir(tmp_path, "mctl-web", 7) / pr_adoption.PRREF_FILENAME
    pr_adoption.write_prref(
        path, "review-fixing",
        kind=pr_adoption.PRREF_KIND, repo="mctlhq/mctl-web", number=7,
        pr="https://github.com/mctlhq/mctl-web/pull/7", head_sha=HEAD_SHA,
        head_branch="chore/manual", owner_type="shepherd",
        review_attempts=1, harness_failures=0, refusals=0,
    )
    before = path.read_text(encoding="utf-8")

    with patch.object(pr_adoption, "_list_open_prs", side_effect=AssertionError("must not re-list; already adopted")):
        refs = pr_adoption.discover_adoptable(tmp_path)

    assert len(refs) == 1
    assert refs[0].review_attempts == 1
    assert refs[0].status == "review-fixing"
    assert path.read_text(encoding="utf-8") == before


def test_discover_adoptable_skips_terminal_records(tmp_path, monkeypatch) -> None:
    _discover_env(monkeypatch)
    path = pr_adoption.record_dir(tmp_path, "mctl-web", 7) / pr_adoption.PRREF_FILENAME
    pr_adoption.write_prref(
        path, "merged",
        kind=pr_adoption.PRREF_KIND, repo="mctlhq/mctl-web", number=7,
        pr="https://github.com/mctlhq/mctl-web/pull/7",
    )
    monkeypatch.setattr(pr_adoption, "_list_open_prs", lambda repo: [])
    refs = pr_adoption.discover_adoptable(tmp_path)
    assert refs == []


def test_discover_adoptable_does_not_readopt_review_stuck_pr(tmp_path, monkeypatch) -> None:
    """A PR already flipped to the terminal `review-stuck` status must stay
    put: `_existing_records` (correctly) excludes terminal records from the
    driven set, but the re-adoption guard still has to see them, or the
    human-triage terminal state never sticks and the fix loop runs unbounded
    (mctlhq/mctl-agents#334 code review)."""
    _discover_env(monkeypatch)
    path = pr_adoption.record_dir(tmp_path, "mctl-web", 7) / pr_adoption.PRREF_FILENAME
    pr_adoption.write_prref(
        path, "review-stuck",
        kind=pr_adoption.PRREF_KIND, repo="mctlhq/mctl-web", number=7,
        pr="https://github.com/mctlhq/mctl-web/pull/7", head_sha=HEAD_SHA,
        head_branch="chore/manual", owner_type="shepherd",
        review_attempts=5, harness_failures=0, refusals=0,
    )
    before = path.read_text(encoding="utf-8")
    monkeypatch.setattr(
        pr_adoption, "_list_open_prs", lambda repo: [_node(7, head_branch="chore/manual")],
    )
    monkeypatch.setattr(run_shepherd, "_fetch_pr_snapshot", lambda repo, number: make_pr(number=number, repo=repo))
    monkeypatch.setattr(
        run_shepherd, "read_codex_review",
        lambda pr: run_shepherd.CodexReview(has_responded=True, findings=[make_finding()]),
    )

    refs = pr_adoption.discover_adoptable(tmp_path)

    assert refs == []
    assert path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# run_implementer wiring (task 7/8)
# ---------------------------------------------------------------------------
def test_build_adopted_ref_missing_record_exits_2(tmp_path) -> None:
    with pytest.raises(SystemExit) as exc:
        run_implementer.build_adopted_ref(tmp_path, "https://github.com/mctlhq/mctl-web/pull/7")
    assert exc.value.code == 2


def test_build_adopted_ref_reads_the_record(tmp_path) -> None:
    path = pr_adoption.record_dir(tmp_path, "mctl-web", 7) / pr_adoption.PRREF_FILENAME
    pr_adoption.write_prref(
        path, "adopted",
        kind=pr_adoption.PRREF_KIND, repo="mctlhq/mctl-web", number=7,
        pr="https://github.com/mctlhq/mctl-web/pull/7", head_branch="chore/manual",
    )
    ref = run_implementer.build_adopted_ref(tmp_path, "https://github.com/mctlhq/mctl-web/pull/7")
    assert ref.service == "mctl-web"
    assert ref.slug == "pr-7"
    assert ref.status == "adopted"
    assert ref.status_path == path
    assert ref.proposal_dir == path.parent


def test_build_adopted_ref_never_creates_a_record(tmp_path) -> None:
    with pytest.raises(SystemExit):
        run_implementer.build_adopted_ref(tmp_path, "https://github.com/mctlhq/mctl-web/pull/7")
    assert not (tmp_path / "mctl-web").exists()


def test_adopted_pr_is_fork_true_on_head_repo_fork_flag(monkeypatch) -> None:
    monkeypatch.setattr(
        run_implementer, "_github_json",
        lambda cmd: {
            "head": {"repo": {"fork": True, "owner": {"login": "someone"}}},
            "base": {"repo": {"owner": {"login": "mctlhq"}}},
        },
    )
    assert run_implementer._adopted_pr_is_fork("mctlhq/mctl-web", 7) is True


def test_adopted_pr_is_fork_true_on_owner_mismatch(monkeypatch) -> None:
    monkeypatch.setattr(
        run_implementer, "_github_json",
        lambda cmd: {
            "head": {"repo": {"fork": False, "owner": {"login": "someone-else"}}},
            "base": {"repo": {"owner": {"login": "mctlhq"}}},
        },
    )
    assert run_implementer._adopted_pr_is_fork("mctlhq/mctl-web", 7) is True


def test_adopted_pr_is_fork_false_for_same_repo(monkeypatch) -> None:
    monkeypatch.setattr(
        run_implementer, "_github_json",
        lambda cmd: {
            "head": {"repo": {"fork": False, "owner": {"login": "mctlhq"}}},
            "base": {"repo": {"owner": {"login": "mctlhq"}}},
        },
    )
    assert run_implementer._adopted_pr_is_fork("mctlhq/mctl-web", 7) is False


def test_main_adopted_pr_exits_2_on_fork_before_cloning(tmp_path, monkeypatch, capsys) -> None:
    """T2 (implementer half): --adopted-pr on a fork exits 2 before
    _clone_target is ever called."""
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("{}", encoding="utf-8")

    monkeypatch.setattr(
        "sys.argv",
        [
            "run_implementer", "--service", "mctl-web",
            "--review-feedback", str(bundle_path),
            "--adopted-pr", "https://github.com/mctlhq/mctl-web/pull/7",
        ],
    )
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(run_implementer, "_adopted_pr_is_fork", lambda *a, **kw: True)
    with patch.object(
        run_implementer, "_clone_target",
        side_effect=AssertionError("must not clone a fork"),
    ), pytest.raises(SystemExit) as exc:
        run_implementer.main()
    assert exc.value.code == 2
    assert "fork" in capsys.readouterr().err.lower()


def test_main_rejects_adopted_pr_with_slug(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        [
            "run_implementer", "--service", "mctl-web", "--slug", "issue-1-foo",
            "--review-feedback", "/tmp/bundle.json",
            "--adopted-pr", "https://github.com/mctlhq/mctl-web/pull/7",
        ],
    )
    with pytest.raises(SystemExit) as exc:
        run_implementer.main()
    assert exc.value.code == 2


def test_main_rejects_adopted_pr_without_review_feedback(monkeypatch) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["run_implementer", "--service", "mctl-web", "--adopted-pr", "https://github.com/mctlhq/mctl-web/pull/7"],
    )
    with pytest.raises(SystemExit) as exc:
        run_implementer.main()
    assert exc.value.code == 2


def test_build_prompt_adopted_variant_drops_proposal_trailer() -> None:
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="pr-7", proposal_dir=Path("/tmp/pr-7"), status="adopted",
    )
    prompt = run_implementer._build_prompt(
        ref, review_feedback={"summaries": ["fix it"]}, branch="chore/manual", adopted=True,
    )
    assert "$PROPOSAL_DIR" in prompt  # still mentioned, but framed as "not a spec"
    assert "requirements.md, design.md, tasks.md" not in prompt
    assert "Proposal: platform-gitops/agents-state/" not in prompt
    assert "PR: https://github.com/mctlhq/mctl-web/pull/7" in prompt
    assert "fix(review): address P1/P2 findings on mctlhq/mctl-web#7" in prompt
    assert "chore/manual" in prompt


def test_build_prompt_backward_compat_pin() -> None:
    """T8: branch=None and adopted=False (the defaults) reproduce today's
    exact output."""
    ref = run_implementer.ProposalRef(
        service="mctl-web", slug="test-slug", proposal_dir=Path("/tmp/test-slug"), status="implemented",
    )
    bundle = {"summaries": ["fix it"]}
    pinned = run_implementer._build_prompt(ref, review_feedback=bundle)
    explicit = run_implementer._build_prompt(ref, review_feedback=bundle, branch=None, adopted=False)
    assert pinned == explicit
    assert "feat/agents-test-slug" in pinned
    assert "Proposal: platform-gitops/agents-state/mctl-web/proposals/test-slug/" in pinned
