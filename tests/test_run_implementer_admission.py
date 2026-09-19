"""The implementer must refuse an accepted proposal whose source issue is
closed, before spending a model attempt (mctl-agents#410).

These tests assert on the REFUSAL and on what did NOT happen -- the whole
point of the admission gate is the spend it prevents, so several of these
mock `ensure_auth_for_sdk` / `_acquire_claim` / `_clone_target` / the SDK
entry point purely to assert they were never called.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import yaml

from orchestrator import run_implementer
from orchestrator.source_issue import SourceIssueVerdict

CLOSED_COMPLETED = SourceIssueVerdict(
    known=True,
    failure={
        "code": "source-resolved",
        "stage": "admission",
        "message": "reconcile-shaped message, unused by the admission gate",
    },
    issue_ref="mctlhq/mctl-telegram#510",
    closed_at="2026-09-06T00:00:00Z",
    state_reason="completed",
)

CLOSED_NOT_PLANNED = SourceIssueVerdict(
    known=True,
    failure={
        "code": "source-not-planned",
        "stage": "admission",
        "message": "reconcile-shaped message, unused by the admission gate",
    },
    issue_ref="mctlhq/mctl-telegram#510",
    closed_at="2026-09-06T00:00:00Z",
    state_reason="not_planned",
)

OPEN = SourceIssueVerdict(known=True, failure=None, issue_ref="mctlhq/mctl-telegram#510")
UNKNOWN_LINKED = SourceIssueVerdict(known=False, failure=None, linked=True)
UNLINKED = SourceIssueVerdict(known=False, failure=None, linked=False)


def write_proposal(state_dir: Path, service: str, slug: str, payload: dict) -> Path:
    d = state_dir / service / "proposals" / slug
    d.mkdir(parents=True)
    (d / ".status.yaml").write_text(yaml.safe_dump(payload), encoding="utf-8")
    return d


def make_ref(tmp_path: Path, *, source=True, slug: str = "issue-x") -> run_implementer.ProposalRef:
    payload: dict = {"status": "accepted"}
    if source:
        payload["source"] = {
            "type": "github_issue",
            "repo": "mctlhq/mctl-telegram",
            "issue": 510,
            "url": "https://github.com/mctlhq/mctl-telegram/issues/510",
        }
    write_proposal(tmp_path, "mctl-telegram", slug, payload)
    return run_implementer.ProposalRef(
        service="mctl-telegram",
        slug=slug,
        proposal_dir=tmp_path / "mctl-telegram" / "proposals" / slug,
        status="accepted",
        approval_ok=True,
    )


def read_status(ref: run_implementer.ProposalRef) -> dict:
    return yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))


def _no_spend_guards(monkeypatch):
    """Patch every model-spending step to explode if reached."""
    def boom(name):
        def _raise(*_a, **_kw):
            raise AssertionError(f"{name} must not be called")
        return _raise

    auth = mock.Mock(side_effect=boom("ensure_auth_for_sdk"))
    claim = mock.Mock(side_effect=boom("_acquire_claim"))
    clone = mock.Mock(side_effect=boom("_clone_target"))
    sdk = mock.Mock(side_effect=boom("anyio.run (the SDK entrypoint)"))
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", auth)
    monkeypatch.setattr(run_implementer, "_acquire_claim", claim)
    monkeypatch.setattr(run_implementer, "_clone_target", clone)
    monkeypatch.setattr(run_implementer.anyio, "run", sdk)
    return auth, claim, clone, sdk


def _stub_preflight_none(monkeypatch):
    monkeypatch.setattr(
        run_implementer,
        "_preflight_existing_result",
        lambda _ref: run_implementer.ExistingResult(action="none"),
    )


# --- closed source issue: refused before the model --------------------------

def test_closed_source_issue_is_refused_before_the_model(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    _stub_preflight_none(monkeypatch)
    auth, claim, clone, sdk = _no_spend_guards(monkeypatch)
    monkeypatch.setattr(
        run_implementer, "read_source_issue", lambda *_a, **_kw: CLOSED_COMPLETED
    )

    result = run_implementer.implement_one(ref, dry_run=False)

    status = read_status(ref)
    assert status["status"] == "needs-triage"
    assert status["failure"]["code"] == "source-resolved"
    assert status["failure"]["stage"] == "admission"
    assert "mctlhq/mctl-telegram#510" in status["failure"]["message"]
    assert result.error is not None
    assert result.counts_toward_limit is False
    auth.assert_not_called()
    claim.assert_not_called()
    clone.assert_not_called()
    sdk.assert_not_called()


def test_not_planned_issue_uses_its_own_code(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    _stub_preflight_none(monkeypatch)
    _no_spend_guards(monkeypatch)
    monkeypatch.setattr(
        run_implementer, "read_source_issue", lambda *_a, **_kw: CLOSED_NOT_PLANNED
    )

    run_implementer.implement_one(ref, dry_run=False)

    assert read_status(ref)["failure"]["code"] == "source-not-planned"


def test_stale_source_result_carries_the_code_and_issue_ref(
    monkeypatch, tmp_path: Path
) -> None:
    ref = make_ref(tmp_path)
    _stub_preflight_none(monkeypatch)
    _no_spend_guards(monkeypatch)
    monkeypatch.setattr(
        run_implementer, "read_source_issue", lambda *_a, **_kw: CLOSED_COMPLETED
    )

    result = run_implementer.implement_one(ref, dry_run=False)

    assert result.stale_source == ("source-resolved", "mctlhq/mctl-telegram#510")


# --- unreadable GitHub: leave the proposal accepted and untouched -----------

def test_unreadable_github_leaves_the_proposal_accepted(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    _stub_preflight_none(monkeypatch)
    auth, claim, clone, sdk = _no_spend_guards(monkeypatch)
    monkeypatch.setattr(
        run_implementer, "read_source_issue", lambda *_a, **_kw: UNKNOWN_LINKED
    )
    before = ref.status_path.read_bytes()

    result = run_implementer.implement_one(ref, dry_run=False)

    assert ref.status_path.read_bytes() == before
    assert result.skipped_reason is not None
    assert result.counts_toward_limit is False
    assert result.error is None
    auth.assert_not_called()
    claim.assert_not_called()
    clone.assert_not_called()
    sdk.assert_not_called()


# --- no source block: the gate must not strand that class of proposal -------

def test_proposal_without_a_source_block_still_runs(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path, source=False)
    _stub_preflight_none(monkeypatch)
    monkeypatch.setattr(
        run_implementer, "read_source_issue", lambda *_a, **_kw: UNLINKED
    )
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda: None)

    reached: dict = {}

    def fake_acquire(*_a, **_kw):
        reached["claim"] = True
        raise run_implementer.ImplementerClaimRefused("stop here on purpose")

    monkeypatch.setattr(run_implementer, "_acquire_claim", fake_acquire)

    result = run_implementer.implement_one(ref, dry_run=False)

    assert reached.get("claim") is True
    # Refused by the claim stub, not by the source-issue gate -- and not
    # charged against the batch budget, exactly like any other claim race.
    assert result.counts_toward_limit is False
    assert "stop here on purpose" in (result.skipped_reason or "")


# --- an existing merged PR wins over a closed issue --------------------------

def test_existing_merged_pr_wins_over_a_closed_issue(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setattr(
        run_implementer,
        "_preflight_existing_result",
        lambda _ref: run_implementer.ExistingResult(
            action="merged",
            pr_url="https://github.com/mctlhq/mctl-telegram/pull/540",
            head_sha="abc123",
        ),
    )
    read_source = mock.Mock(side_effect=AssertionError("source issue must not be read"))
    monkeypatch.setattr(run_implementer, "read_source_issue", read_source)

    result = run_implementer.implement_one(ref, dry_run=False)

    assert read_status(ref)["status"] == "merged"
    assert result.pr_url == "https://github.com/mctlhq/mctl-telegram/pull/540"
    read_source.assert_not_called()


# --- a refusal does not consume the batch budget -----------------------------

def test_refusal_does_not_consume_the_batch_budget(monkeypatch, tmp_path: Path) -> None:
    first = make_ref(tmp_path, slug="first")
    second = make_ref(tmp_path, slug="second")

    def fake_implement(ref, dry_run=False):
        if ref.slug == "first":
            return run_implementer.ImplementResult(
                ref=ref, pr_url=None, error="source-resolved",
                counts_toward_limit=False,
                stale_source=("source-resolved", "mctlhq/mctl-telegram#510"),
            )
        return run_implementer.ImplementResult(
            ref=ref, pr_url="https://github.com/mctlhq/mctl-telegram/pull/99",
        )

    monkeypatch.setattr(run_implementer, "implement_one", fake_implement)
    results = run_implementer._implement_refs(
        [first, second], max_proposals=1, dry_run=False,
    )
    assert [r.ref.slug for r in results] == ["first", "second"]


# --- supersession evidence ---------------------------------------------------

def test_supersession_urls_are_listed_in_the_message(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setattr(
        run_implementer, "_superseding_pr_urls",
        lambda *_a, **_kw: ["https://github.com/mctlhq/mctl-telegram/pull/540"],
    )

    message = run_implementer._stale_source_message(ref, CLOSED_COMPLETED)

    assert "Superseded by: https://github.com/mctlhq/mctl-telegram/pull/540" in message
    assert "mctlhq/mctl-telegram#510" in message
    assert "2026-09-06T00:00:00Z" in message
    assert "reopen" in message.lower() or "Reopen" in message
    assert "proposed" in message


def test_superseding_pr_urls_never_propagates_and_never_changes_the_verdict(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        run_implementer, "_github_json",
        lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("gh api 502")),
    )
    urls = run_implementer._superseding_pr_urls("mctlhq/mctl-telegram", 510)
    assert urls == []


def test_superseding_pr_urls_empty_timeline_returns_nothing(monkeypatch) -> None:
    monkeypatch.setattr(run_implementer, "_github_json", lambda *_a, **_kw: [])
    assert run_implementer._superseding_pr_urls("mctlhq/mctl-telegram", 510) == []


def test_superseding_pr_urls_reads_cross_referenced_merged_events(monkeypatch) -> None:
    timeline = [
        {"event": "cross-referenced", "source": {"issue": {
            "html_url": "https://github.com/mctlhq/mctl-telegram/pull/540",
            "pull_request": {"merged_at": "2026-09-10T00:00:00Z"},
        }}},
        {"event": "commented"},  # ignored -- not a signal either way
        {"event": "cross-referenced", "source": {"issue": {
            "html_url": "https://github.com/mctlhq/mctl-telegram/pull/500",
            "pull_request": {"merged_at": None},  # not merged -- ignored
        }}},
    ]
    monkeypatch.setattr(run_implementer, "_github_json", lambda *_a, **_kw: timeline)
    urls = run_implementer._superseding_pr_urls("mctlhq/mctl-telegram", 510)
    assert urls == ["https://github.com/mctlhq/mctl-telegram/pull/540"]


def test_superseding_pr_urls_sorts_newest_first_and_caps_at_three(monkeypatch) -> None:
    def cross_ref(number, merged_at):
        return {"event": "cross-referenced", "source": {"issue": {
            "html_url": f"https://github.com/mctlhq/mctl-telegram/pull/{number}",
            "pull_request": {"merged_at": merged_at},
        }}}

    timeline = [
        cross_ref(500, "2026-01-01T00:00:00Z"),
        cross_ref(510, "2026-09-10T00:00:00Z"),
        cross_ref(505, "2026-05-01T00:00:00Z"),
        cross_ref(520, "2026-09-15T00:00:00Z"),
    ]
    monkeypatch.setattr(run_implementer, "_github_json", lambda *_a, **_kw: timeline)
    urls = run_implementer._superseding_pr_urls("mctlhq/mctl-telegram", 510)
    assert urls == [
        "https://github.com/mctlhq/mctl-telegram/pull/520",
        "https://github.com/mctlhq/mctl-telegram/pull/510",
        "https://github.com/mctlhq/mctl-telegram/pull/505",
    ]


def test_superseding_pr_urls_looks_up_closed_events_with_a_commit_id(monkeypatch) -> None:
    timeline = [{"event": "closed", "commit_id": "abc123"}]

    def fake_github_json(cmd):
        if "timeline" in cmd[-1]:
            return timeline
        assert "commits/abc123/pulls" in cmd[-1]
        return [{"html_url": "https://github.com/mctlhq/mctl-telegram/pull/540",
                  "merged_at": "2026-09-10T00:00:00Z"}]

    monkeypatch.setattr(run_implementer, "_github_json", fake_github_json)
    urls = run_implementer._superseding_pr_urls("mctlhq/mctl-telegram", 510)
    assert urls == ["https://github.com/mctlhq/mctl-telegram/pull/540"]


def test_superseding_pr_urls_a_bad_commit_lookup_does_not_drop_other_urls(
    monkeypatch,
) -> None:
    timeline = [
        {"event": "cross-referenced", "source": {"issue": {
            "html_url": "https://github.com/mctlhq/mctl-telegram/pull/540",
            "pull_request": {"merged_at": "2026-09-10T00:00:00Z"},
        }}},
        {"event": "closed", "commit_id": "deadbeef"},
    ]

    def fake_github_json(cmd):
        if "timeline" in cmd[-1]:
            return timeline
        raise RuntimeError("commit lookup 502")

    monkeypatch.setattr(run_implementer, "_github_json", fake_github_json)
    urls = run_implementer._superseding_pr_urls("mctlhq/mctl-telegram", 510)
    assert urls == ["https://github.com/mctlhq/mctl-telegram/pull/540"]


def test_not_planned_never_looks_up_supersession(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    lookup = mock.Mock(side_effect=AssertionError("must not run for not_planned"))
    monkeypatch.setattr(run_implementer, "_superseding_pr_urls", lookup)

    message = run_implementer._stale_source_message(ref, CLOSED_NOT_PLANNED)

    lookup.assert_not_called()
    assert "Superseded by" not in message


# --- dry-run writes nothing ---------------------------------------------------

def test_dry_run_writes_nothing(monkeypatch, tmp_path: Path) -> None:
    ref = make_ref(tmp_path)
    before = ref.status_path.read_bytes()
    monkeypatch.setattr(
        run_implementer, "read_source_issue", lambda *_a, **_kw: CLOSED_COMPLETED
    )

    result = run_implementer.implement_one(ref, dry_run=True)

    # dry_run returns before the preflight/gate today; unaffected by this
    # change, and the gate must not somehow run underneath it.
    assert ref.status_path.read_bytes() == before
    assert result.skipped_reason == "dry-run"


# --- === Stale source === summary section ------------------------------------

def test_main_prints_a_stale_source_section(monkeypatch, tmp_path: Path, capsys) -> None:
    ref = make_ref(tmp_path)
    result = run_implementer.ImplementResult(
        ref=ref, pr_url=None, error="source-resolved",
        counts_toward_limit=False,
        stale_source=("source-resolved", "mctlhq/mctl-telegram#510"),
    )
    monkeypatch.setattr(
        run_implementer, "find_accepted_proposals", lambda *_a, **_kw: [ref]
    )
    monkeypatch.setattr(
        run_implementer, "_implement_refs", lambda *_a, **_kw: [result]
    )
    monkeypatch.setattr(
        "sys.argv", ["run_implementer.py", "--state-dir", str(tmp_path)]
    )

    # A refusal-only batch no longer raises SystemExit (codex P2 follow-up on
    # mctl-agents#410, PR #416): the `needs-triage` write must reach the
    # downstream commit-and-push step, which is gated on this step's Argo
    # status rather than its exit code.
    run_implementer.main()

    output = capsys.readouterr().out
    assert "=== Stale source ===" in output
    assert "mctl-telegram/issue-x: source-resolved mctlhq/mctl-telegram#510" in output


def test_main_omits_the_stale_source_section_when_there_is_none(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    ref = make_ref(tmp_path)
    result = run_implementer.ImplementResult(
        ref=ref, pr_url="https://github.com/mctlhq/mctl-telegram/pull/99",
    )
    monkeypatch.setattr(
        run_implementer, "find_accepted_proposals", lambda *_a, **_kw: [ref]
    )
    monkeypatch.setattr(
        run_implementer, "_implement_refs", lambda *_a, **_kw: [result]
    )
    monkeypatch.setattr(
        "sys.argv", ["run_implementer.py", "--state-dir", str(tmp_path)]
    )

    run_implementer.main()

    output = capsys.readouterr().out
    assert "=== Stale source ===" not in output
