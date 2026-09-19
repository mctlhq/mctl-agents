"""Unit tests for the shared quota-exhaustion classifier (mctl-agents#364).

`orchestrator.rate_limit` must stay import-safe for the Temporal worker (see
tests/test_worker_isolation.py) — every check here is duck-typed, so plain
attribute-bearing stand-ins exercise it exactly like the real SDK classes
would, without importing `claude_agent_sdk` at all.
"""
from __future__ import annotations

import types

from orchestrator import rate_limit


class _ResultMessage:
    def __init__(self, *, is_error, api_error_status):
        self.is_error = is_error
        self.api_error_status = api_error_status


class _RateLimitEvent:
    def __init__(self, *, status, resets_at=None, rate_limit_type=None, overage_disabled_reason=None):
        self.rate_limit_info = _RateLimitInfo(
            status=status, resets_at=resets_at, rate_limit_type=rate_limit_type,
            overage_disabled_reason=overage_disabled_reason,
        )


class _RateLimitInfo:
    def __init__(self, *, status, resets_at, rate_limit_type, overage_disabled_reason):
        self.status = status
        self.resets_at = resets_at
        self.rate_limit_type = rate_limit_type
        self.overage_disabled_reason = overage_disabled_reason


def test_is_rate_limit_result_true_on_429():
    message = _ResultMessage(is_error=True, api_error_status=429)
    message.__class__.__name__ = "ResultMessage"
    assert rate_limit.is_rate_limit_result(message) is True


def test_is_rate_limit_result_false_on_500():
    message = _ResultMessage(is_error=True, api_error_status=500)
    message.__class__.__name__ = "ResultMessage"
    assert rate_limit.is_rate_limit_result(message) is False


def test_is_rate_limit_result_false_on_clean_result():
    message = _ResultMessage(is_error=False, api_error_status=None)
    message.__class__.__name__ = "ResultMessage"
    assert rate_limit.is_rate_limit_result(message) is False


def test_is_rate_limit_result_false_on_wrong_type_name():
    """Duck typing keys on the class name too, not just the attributes —
    an unrelated object that happens to carry the same field names must not
    trip the classifier."""
    message = types.SimpleNamespace(is_error=True, api_error_status=429)
    assert rate_limit.is_rate_limit_result(message) is False


def test_observe_rate_limit_event_extracts_info():
    event = _RateLimitEvent(status="rejected", resets_at=100, rate_limit_type="seven_day")
    event.__class__.__name__ = "RateLimitEvent"
    info = rate_limit.observe_rate_limit_event(event)
    assert info is not None
    assert info.status == "rejected"
    assert info.rate_limit_type == "seven_day"


def test_observe_rate_limit_event_ignores_other_messages():
    assert rate_limit.observe_rate_limit_event("just a string") is None
    assert rate_limit.observe_rate_limit_event(_ResultMessage(is_error=False, api_error_status=None)) is None


def test_account_label_reads_explicit_env_var(monkeypatch):
    monkeypatch.setenv("CLAUDE_OAUTH_ACCOUNT", "2")
    assert rate_limit.account_label() == "2"


def test_account_label_falls_back_to_primary_from_token_env_var(monkeypatch):
    monkeypatch.delenv("CLAUDE_OAUTH_ACCOUNT", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-fake")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert rate_limit.account_label() == "primary"


def test_account_label_falls_back_to_secondary_from_token_env_var(monkeypatch):
    monkeypatch.delenv("CLAUDE_OAUTH_ACCOUNT", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "sk-ant-oat01-fake")
    assert rate_limit.account_label() == "secondary"


def test_account_label_is_unknown_with_no_auth_configured(monkeypatch):
    import shutil

    for var in (
        "CLAUDE_OAUTH_ACCOUNT",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_API_KEY_SECONDARY",
    ):
        monkeypatch.delenv(var, raising=False)
    # detect_auth() falls back to an existing `claude` CLI session (still no
    # env var to derive an account label from) before finally raising; force
    # both branches to the account-less answer this test is about.
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert rate_limit.account_label() == "unknown"


def test_account_label_never_returns_a_token_value(monkeypatch):
    """However an account is derived, the label itself must never BE the
    credential (mctl-agents#364's no-credential-material requirement)."""
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-super-secret-value")
    label = rate_limit.account_label()
    assert "sk-ant-oat01-super-secret-value" not in label


def test_build_observation_with_info():
    event = _RateLimitEvent(
        status="rejected", resets_at=1789509600, rate_limit_type="seven_day",
        overage_disabled_reason="out_of_credits",
    )
    observation = rate_limit.build_observation(event.rate_limit_info, detail="d")
    assert observation.rate_limit_type == "seven_day"
    assert observation.overage_disabled_reason == "out_of_credits"
    assert observation.resets_at_epoch == 1789509600
    assert observation.resets_at == "2026-09-15T22:00:00Z"
    assert observation.detail == "d"


def test_build_observation_without_info():
    observation = rate_limit.build_observation(None, detail="no event seen")
    assert observation.rate_limit_type is None
    assert observation.resets_at is None
    assert observation.resets_at_epoch is None
    assert observation.overage_disabled_reason is None
    assert observation.detail == "no event seen"


def test_rate_limit_exhausted_error_carries_optional_observation():
    bare = rate_limit.RateLimitExhaustedError("boom")
    assert bare.observation is None

    observation = rate_limit.build_observation(None, detail="d")
    carrying = rate_limit.RateLimitExhaustedError("boom", observation)
    assert carrying.observation is observation
