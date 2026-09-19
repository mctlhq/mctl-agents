"""Quota-exhaustion classification for the Tier 2 implementer (mctl-agents#364).

Mirrors tests/test_run_issue_investigator.py's rate-limit coverage (the
sibling agent that already got this right) and
tests/test_run_implementer_timeout.py's `_fake_client_factory` pattern for
driving `_run_implementer_agent` directly.
"""
from __future__ import annotations

from pathlib import Path

import anyio
import pytest
import yaml

from orchestrator import run_implementer
from orchestrator.rate_limit import RateLimitObservation
from tests.conftest import result_message


# ---------------------------------------------------------------------------
# Fakes — same shape as test_run_implementer_timeout.py's, local so this file
# has no cross-file import dependency.
# ---------------------------------------------------------------------------
class _FakeClient:
    def __init__(self, *, options, message_gen):
        self._message_gen = message_gen

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def query(self, prompt):
        pass

    async def receive_response(self):
        async for message in self._message_gen():
            yield message

    async def receive_messages(self):
        async for message in self._message_gen():
            yield message


def _fake_client_factory(message_gen):
    def _factory(*, options):
        return _FakeClient(options=options, message_gen=message_gen)
    return _factory


class RateLimitEvent:
    """Duck-typed stand-in for claude_agent_sdk.RateLimitEvent — the module
    under test never imports the SDK, so its own check is purely on
    `type(message).__name__` and `getattr`, and a plain class with the right
    name and attribute satisfies it exactly like the real one would."""

    def __init__(self, *, status, resets_at=None, rate_limit_type=None, overage_disabled_reason=None):
        self.rate_limit_info = type(
            "RateLimitInfo",
            (),
            {
                "status": status,
                "resets_at": resets_at,
                "rate_limit_type": rate_limit_type,
                "overage_disabled_reason": overage_disabled_reason,
            },
        )()


# ---------------------------------------------------------------------------
# T1/T2 — _run_implementer_agent classification
# ---------------------------------------------------------------------------
def test_agent_raises_on_429_result(tmp_path, monkeypatch) -> None:
    async def messages():
        yield RateLimitEvent(
            status="rejected", resets_at=1789509600,
            rate_limit_type="seven_day", overage_disabled_reason="out_of_credits",
        )
        yield result_message(is_error=True, api_error_status=429)

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)

    with pytest.raises(run_implementer.RateLimitExhaustedError) as exc_info:
        anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)

    observation = exc_info.value.observation
    assert observation is not None
    assert observation.rate_limit_type == "seven_day"
    assert observation.overage_disabled_reason == "out_of_credits"
    assert observation.resets_at_epoch == 1789509600
    assert observation.resets_at is not None


def test_agent_does_not_raise_on_clean_result(tmp_path, monkeypatch) -> None:
    async def messages():
        yield result_message(is_error=False)

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)

    anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)


def test_agent_does_not_raise_on_500(tmp_path, monkeypatch) -> None:
    """A different api_error_status must still reach the generic failure
    path — mirrors tests/test_run_issue_investigator.py's 500 case."""
    async def messages():
        yield result_message(is_error=True, api_error_status=500)

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)

    anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)


def test_agent_classifies_a_rate_limit_seen_during_the_drain(tmp_path, monkeypatch) -> None:
    """A 429 hit while awaiting a delegated child must not be lost — mirrors
    the loss mctl-agents#366 fixed for orphaned sub-agents."""
    from claude_agent_sdk import TaskStartedMessage

    async def messages():
        yield TaskStartedMessage(
            subtype="task_started", data={}, task_id="t1",
            description="work", uuid="u", session_id="s", task_type="local_agent",
        )
        yield result_message(is_error=False)  # turn 1 ends, child still live
        yield result_message(is_error=True, api_error_status=429)  # hit during drain

    monkeypatch.setattr(run_implementer, "ClaudeSDKClient", _fake_client_factory(messages))
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_TIMEOUT_SECONDS", 5)
    monkeypatch.setattr(run_implementer, "IMPLEMENTER_DRAIN_TIMEOUT_SECONDS", 5)

    with pytest.raises(run_implementer.RateLimitExhaustedError):
        anyio.run(run_implementer._run_implementer_agent, tmp_path, "prompt", tmp_path)


# ---------------------------------------------------------------------------
# implement_one() classification — same harness as
# tests/test_run_implementer_idempotency.py's `make_ref`/`read_status`.
# ---------------------------------------------------------------------------
def make_ref(tmp_path: Path, status: str = "accepted") -> run_implementer.ProposalRef:
    proposal = tmp_path / "mctl-agents" / "proposals" / "rate-limited-example"
    proposal.mkdir(parents=True)
    (proposal / ".status.yaml").write_text(
        yaml.safe_dump({"status": status}), encoding="utf-8",
    )
    return run_implementer.ProposalRef(
        service="mctl-agents",
        slug="rate-limited-example",
        proposal_dir=proposal,
        status=status,
        approval_ok=True,
    )


def read_status(ref: run_implementer.ProposalRef) -> dict:
    return yaml.safe_load(ref.status_path.read_text(encoding="utf-8"))


def _rig_implement_one_up_to_the_sdk_call(monkeypatch, tmp_path: Path, *, raiser) -> None:
    """Stub every step before `anyio.run(_run_implementer_agent, ...)` so
    `implement_one` reaches the SDK call and `raiser` decides what happens
    there — the same technique
    test_run_implementer_idempotency.py::test_batch_mode_orphaned_subagent_marks_its_own_triage_code
    uses for `ImplementerOrphanedSubagent`.
    """
    monkeypatch.setattr(run_implementer, "ensure_auth_for_sdk", lambda: None)
    monkeypatch.setattr(
        run_implementer, "_preflight_existing_result",
        lambda _ref: run_implementer.ExistingResult(action="none"),
    )
    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a: tmp_path / "clone")
    monkeypatch.setattr(run_implementer, "_run", lambda *_a, **_kw: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(run_implementer.anyio, "run", raiser)


def _observation(**overrides) -> RateLimitObservation:
    fields = dict(
        account="primary",
        rate_limit_type="seven_day",
        resets_at="2026-09-26T00:00:00Z",
        resets_at_epoch=1789509600,
        overage_disabled_reason="out_of_credits",
        detail="terminal ResultMessage api_error_status=429",
    )
    fields.update(overrides)
    return RateLimitObservation(**fields)


def _raise_rate_limited(observation: RateLimitObservation | None = None):
    def _raiser(*_a, **_kw):
        raise run_implementer.RateLimitExhaustedError(
            "SDK reported api_error_status=429 (rate/usage limit exhausted): 'boom'",
            observation or _observation(),
        )
    return _raiser


def test_rate_limited_run_leaves_status_accepted(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    _rig_implement_one_up_to_the_sdk_call(monkeypatch, tmp_path, raiser=_raise_rate_limited())

    result = run_implementer.implement_one(ref)

    assert result.rate_limited is True
    assert result.counts_toward_limit is False
    status = read_status(ref)
    assert status["status"] == "accepted"
    assert "attempt" not in status
    assert "failure" not in status
    assert status["rate_limited"]["code"] == "rate-limited"
    assert status["rate_limited"]["account"] == "primary"
    assert status["rate_limited"]["rate_limit_type"] == "seven_day"


def test_rate_limited_run_is_not_recorded_as_no_commits(tmp_path, monkeypatch) -> None:
    """The regression this issue is about."""
    ref = make_ref(tmp_path)
    _rig_implement_one_up_to_the_sdk_call(monkeypatch, tmp_path, raiser=_raise_rate_limited())

    run_implementer.implement_one(ref)

    status = read_status(ref)
    assert "failure" not in status
    serialised = yaml.safe_dump(status)
    assert "no-commits" not in serialised
    assert "implementer produced no commits" not in serialised


def test_rate_limited_result_does_not_count_toward_limit(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    _rig_implement_one_up_to_the_sdk_call(monkeypatch, tmp_path, raiser=_raise_rate_limited())

    result = run_implementer.implement_one(ref)

    assert result.counts_toward_limit is False


def test_batch_stops_after_first_rate_limited_ref(monkeypatch) -> None:
    first = run_implementer.ProposalRef(
        service="mctl-agents", slug="first", proposal_dir=Path("/tmp/first"),
        status="accepted", approval_ok=True,
    )
    second = run_implementer.ProposalRef(
        service="mctl-agents", slug="second", proposal_dir=Path("/tmp/second"),
        status="accepted", approval_ok=True,
    )
    calls: list[str] = []

    def fake_implement(ref, dry_run=False):
        calls.append(ref.slug)
        return run_implementer.ImplementResult(
            ref=ref, pr_url=None, error="rate limited: boom",
            rate_limited=True, counts_toward_limit=False,
        )

    monkeypatch.setattr(run_implementer, "implement_one", fake_implement)
    results = run_implementer._implement_refs([first, second], max_proposals=1, dry_run=False)

    assert calls == ["first"]
    assert len(results) == 1
    assert results[0].rate_limited is True


# ---------------------------------------------------------------------------
# Idempotent / self-clearing block
# ---------------------------------------------------------------------------
def test_repeated_observation_is_idempotent(tmp_path, monkeypatch) -> None:
    """`_mark_rate_limited` at the unit level: an identical observation
    preserves `since`; a genuinely new one (different `resets_at`) does not.

    Exercised directly against `_mark_rate_limited` rather than through two
    `implement_one` calls: `implement_one`'s OWN `in-progress` write (taken
    before every SDK call, since the outcome is not known yet) already passes
    `rate_limited=None`, so by construction the very next tick's `except
    RateLimitExhaustedError` branch never sees a PRIOR block to compare
    against — see `_mark_rate_limited`'s docstring. What keeps a real,
    continuing outage from restarting `since` on every tick is the task 11
    guard skipping BEFORE that `in-progress` write ever runs (see
    `test_skip_while_window_open_fires_on_matching_account` and
    `test_implement_one_skips_before_clone_when_window_is_open` below) —
    this test isolates the "preserve since when unchanged" rule on its own.
    """
    # `_now_iso()` has one-second resolution, so a bare wall-clock run could
    # produce identical timestamps well within the same second and pass even
    # if `since` were never preserved at all. A fake, strictly increasing
    # clock makes the assertion mean something.
    clock = iter(f"2026-09-19T00:00:{i:02d}Z" for i in range(10))
    monkeypatch.setattr(run_implementer, "_now_iso", lambda: next(clock))

    ref = make_ref(tmp_path)
    observation = _observation()

    # `_mark_rate_limited` now CASes on `.status.yaml`'s `attempt.id` (mirrors
    # `_mark_needs_triage`/`_status_is_still_ours`), so each call below stamps
    # the `attempt` block its own `attempt_id` will match — standing in for
    # the `in-progress` write `implement_one` makes before every SDK call.
    def _stamp(attempt_id: str) -> None:
        run_implementer.update_status_yaml(ref, "in-progress", attempt={"id": attempt_id})

    _stamp("a1")
    run_implementer._mark_rate_limited(
        ref, observation=observation, attempt_id="a1", message="first",
    )
    first_since = read_status(ref)["rate_limited"]["since"]

    # Second call: identical account/type/resets_at must preserve `since`.
    _stamp("a2")
    run_implementer._mark_rate_limited(
        ref, observation=observation, attempt_id="a2", message="second",
    )
    second = read_status(ref)["rate_limited"]
    assert second["since"] == first_since
    assert second["message"] == "second"  # the rest of the block DOES update

    # Third call: a genuinely new observation (different resets_at) must NOT
    # inherit the stale `since`.
    new_observation = _observation(
        resets_at="2026-10-03T00:00:00Z", resets_at_epoch=1790114400,
    )
    _stamp("a3")
    run_implementer._mark_rate_limited(
        ref, observation=new_observation, attempt_id="a3", message="third",
    )
    third = read_status(ref)["rate_limited"]
    assert third["resets_at"] == "2026-10-03T00:00:00Z"
    assert third["since"] != first_since


def test_mark_rate_limited_declines_when_a_second_executor_now_holds_the_proposal(
    tmp_path,
) -> None:
    """The same compare-and-swap `_mark_needs_triage` performs (mctl-agents#364
    P1 follow-up): between this attempt's own `in-progress` write and the
    terminal 429, a second executor can legitimately have taken the proposal
    and stamped its own `attempt` block. Rolling back to `accepted` and
    dropping THAT block here would erase the second executor's hold and let a
    third implementer start — the exact bug `_status_is_still_ours` exists to
    prevent on every other terminal write."""
    ref = make_ref(tmp_path)
    run_implementer.update_status_yaml(
        ref, "in-progress", attempt={"id": "someone-else"},
    )

    result = run_implementer._mark_rate_limited(
        ref, observation=_observation(), attempt_id="ours", message="rate limited",
    )

    assert result is None
    status = read_status(ref)
    assert status["status"] == "in-progress"
    assert status["attempt"]["id"] == "someone-else"
    assert "rate_limited" not in status


def test_rate_limited_block_is_cleared_on_success(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    _rig_implement_one_up_to_the_sdk_call(monkeypatch, tmp_path, raiser=_raise_rate_limited())
    run_implementer.implement_one(ref)
    assert "rate_limited" in read_status(ref)

    monkeypatch.setattr(
        run_implementer, "_preflight_existing_result",
        lambda _ref: run_implementer.ExistingResult(
            action="open", pr_url="https://github.com/mctlhq/mctl-agents/pull/1",
        ),
    )
    result = run_implementer.implement_one(ref)

    assert result.pr_url is not None
    assert "rate_limited" not in read_status(ref)


# ---------------------------------------------------------------------------
# review-feedback mode
# ---------------------------------------------------------------------------
def test_review_feedback_rate_limit_exit_code(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setattr(run_implementer, "_clone_target", lambda *_a: tmp_path / "clone")
    monkeypatch.setattr(run_implementer, "_branch_exists_on_origin", lambda *_a: True)
    monkeypatch.setattr(run_implementer, "_checkout_existing_branch", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_stage_implementer_agent", lambda *_a: None)
    monkeypatch.setattr(run_implementer, "_capture_head_sha", lambda *_a: "abc123")
    monkeypatch.setattr(run_implementer.anyio, "run", _raise_rate_limited())

    result = run_implementer.review_feedback_one(ref, {"p1": False, "p2": False, "summaries": []})

    assert result.rate_limited is True
    assert result.counts_toward_limit is False
    assert result.error.startswith("rate limited:")
    assert run_implementer._review_feedback_exit_code(result.error) == run_implementer.EXIT_RATE_LIMITED
    # The shepherd owns status in this mode — nothing written.
    status = read_status(ref)
    assert status["status"] == "accepted"


# ---------------------------------------------------------------------------
# Batch summary / exit code
# ---------------------------------------------------------------------------
def test_batch_outcome_counts_rate_limited_separately() -> None:
    rate_limited = run_implementer.ImplementResult(
        ref=make_ref_stub("rl"), pr_url=None, error="rate limited: boom",
        rate_limited=True, counts_toward_limit=False,
    )
    outcome = run_implementer._batch_outcome([rate_limited])
    assert outcome == run_implementer.BatchOutcome(
        succeeded=0, failed=0, skipped=0, blocked=0, rate_limited=1,
    )


def make_ref_stub(slug: str) -> run_implementer.ProposalRef:
    return run_implementer.ProposalRef(
        service="mctl-agents", slug=slug, proposal_dir=Path("/tmp") / slug, status="accepted",
    )


def test_main_exits_rate_limited_when_no_success(monkeypatch, tmp_path, capsys) -> None:
    ref = make_ref_stub("rl")
    result = run_implementer.ImplementResult(
        ref=ref, pr_url=None, error="rate limited: boom",
        rate_limited=True, counts_toward_limit=False,
        rate_limit_observation=_observation(),
    )
    monkeypatch.setattr(run_implementer, "find_accepted_proposals", lambda *_a, **_kw: [ref])
    monkeypatch.setattr(run_implementer, "_implement_refs", lambda *_a, **_kw: [result])
    monkeypatch.setattr("sys.argv", ["run_implementer.py", "--state-dir", str(tmp_path)])

    with pytest.raises(SystemExit) as exc_info:
        run_implementer.main()

    assert exc_info.value.code == run_implementer.EXIT_RATE_LIMITED
    err = capsys.readouterr().err
    assert "error: rate-limited: account=primary type=seven_day" in err
    assert "proposal=mctl-agents/rl" in err


def test_main_exits_zero_when_rate_limited_batch_also_succeeded(monkeypatch, tmp_path) -> None:
    rl_ref = make_ref_stub("rl")
    ok_ref = make_ref_stub("ok")
    rl_result = run_implementer.ImplementResult(
        ref=rl_ref, pr_url=None, error="rate limited: boom",
        rate_limited=True, counts_toward_limit=False,
    )
    ok_result = run_implementer.ImplementResult(
        ref=ok_ref, pr_url="https://github.com/mctlhq/mctl-agents/pull/1",
    )
    monkeypatch.setattr(run_implementer, "find_accepted_proposals", lambda *_a, **_kw: [ok_ref, rl_ref])
    monkeypatch.setattr(run_implementer, "_implement_refs", lambda *_a, **_kw: [ok_result, rl_result])
    monkeypatch.setattr("sys.argv", ["run_implementer.py", "--state-dir", str(tmp_path)])

    run_implementer.main()  # must not raise SystemExit


# ---------------------------------------------------------------------------
# No credential material leaks
# ---------------------------------------------------------------------------
def test_no_credential_material_is_recorded(tmp_path, monkeypatch, capsys) -> None:
    sentinel = "sk-ant-oat01-SENTINELDONOTLEAK"
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", sentinel)
    ref = make_ref(tmp_path)
    _rig_implement_one_up_to_the_sdk_call(monkeypatch, tmp_path, raiser=_raise_rate_limited())

    run_implementer.implement_one(ref)

    file_text = ref.status_path.read_text(encoding="utf-8")
    assert sentinel not in file_text
    captured = capsys.readouterr()
    assert sentinel not in captured.out
    assert sentinel not in captured.err


# ---------------------------------------------------------------------------
# T11 — optional skip guard
# ---------------------------------------------------------------------------
def test_skip_while_window_open(tmp_path) -> None:
    ref = make_ref(tmp_path)
    run_implementer.update_status_yaml(
        ref, "accepted",
        rate_limited={
            "code": "rate-limited",
            "account": "primary",
            "rate_limit_type": "seven_day",
            "resets_at": "2099-01-01T00:00:00Z",
            "resets_at_epoch": 4070908800,
            "overage_disabled_reason": None,
            "since": "2026-09-19T00:00:00Z",
            "observed_at": "2026-09-19T00:00:00Z",
            "attempt_id": "x",
            "message": "m",
        },
    )
    assert run_implementer.account_label() == "unknown"  # no auth configured in test env
    # account_label() answers "unknown" without any auth configured, so this
    # guard must fail OPEN here — matching account is required to skip.
    assert run_implementer._skip_while_rate_limited(ref) is None


def test_skip_while_window_open_fires_on_matching_account(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setenv("CLAUDE_OAUTH_ACCOUNT", "primary")
    run_implementer.update_status_yaml(
        ref, "accepted",
        rate_limited={
            "code": "rate-limited",
            "account": "primary",
            "rate_limit_type": "seven_day",
            "resets_at": "2099-01-01T00:00:00Z",
        },
    )
    reason = run_implementer._skip_while_rate_limited(ref)
    assert reason is not None
    assert "primary" in reason
    # No status write happened.
    assert read_status(ref)["rate_limited"]["resets_at"] == "2099-01-01T00:00:00Z"


def test_skip_while_window_open_fails_open_on_mismatched_account(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setenv("CLAUDE_OAUTH_ACCOUNT", "secondary")
    run_implementer.update_status_yaml(
        ref, "accepted",
        rate_limited={
            "code": "rate-limited",
            "account": "primary",
            "rate_limit_type": "seven_day",
            "resets_at": "2099-01-01T00:00:00Z",
        },
    )
    assert run_implementer._skip_while_rate_limited(ref) is None


def test_skip_while_window_open_fails_open_on_unknown_recorded_account(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setenv("CLAUDE_OAUTH_ACCOUNT", "unknown")
    run_implementer.update_status_yaml(
        ref, "accepted",
        rate_limited={
            "code": "rate-limited",
            "account": "unknown",
            "rate_limit_type": "seven_day",
            "resets_at": "2099-01-01T00:00:00Z",
        },
    )
    assert run_implementer._skip_while_rate_limited(ref) is None


def test_skip_while_window_open_fails_open_on_absent_block(tmp_path) -> None:
    ref = make_ref(tmp_path)
    assert run_implementer._skip_while_rate_limited(ref) is None


def test_skip_while_window_open_fails_open_on_unparsable_timestamp(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setenv("CLAUDE_OAUTH_ACCOUNT", "primary")
    run_implementer.update_status_yaml(
        ref, "accepted",
        rate_limited={
            "code": "rate-limited",
            "account": "primary",
            "rate_limit_type": "seven_day",
            "resets_at": "not-a-timestamp",
        },
    )
    assert run_implementer._skip_while_rate_limited(ref) is None


def test_skip_while_window_open_fails_open_once_window_has_passed(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setenv("CLAUDE_OAUTH_ACCOUNT", "primary")
    run_implementer.update_status_yaml(
        ref, "accepted",
        rate_limited={
            "code": "rate-limited",
            "account": "primary",
            "rate_limit_type": "seven_day",
            "resets_at": "2020-01-01T00:00:00Z",
        },
    )
    assert run_implementer._skip_while_rate_limited(ref) is None


def test_implement_one_skips_before_clone_when_window_is_open(tmp_path, monkeypatch) -> None:
    ref = make_ref(tmp_path)
    monkeypatch.setenv("CLAUDE_OAUTH_ACCOUNT", "primary")
    run_implementer.update_status_yaml(
        ref, "accepted",
        rate_limited={
            "code": "rate-limited",
            "account": "primary",
            "rate_limit_type": "seven_day",
            "resets_at": "2099-01-01T00:00:00Z",
        },
    )
    monkeypatch.setattr(
        run_implementer, "_preflight_existing_result",
        lambda _ref: run_implementer.ExistingResult(action="none"),
    )
    monkeypatch.setattr(
        run_implementer, "_clone_target",
        lambda *_a: (_ for _ in ()).throw(AssertionError("must not clone")),
    )

    result = run_implementer.implement_one(ref)

    assert result.skipped_reason is not None
    assert result.counts_toward_limit is False
    assert read_status(ref)["status"] == "accepted"
