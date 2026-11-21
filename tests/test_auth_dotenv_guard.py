"""load_dotenv() at import of orchestrator.auth is skipped in a cluster (#201)."""
from __future__ import annotations

import pytest

from orchestrator import auth


@pytest.fixture
def load_calls(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    calls: list[int] = []
    monkeypatch.setattr(auth, "load_dotenv", lambda *a, **k: calls.append(1))
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    monkeypatch.delenv("MCTL_AGENTS_SKIP_DOTENV", raising=False)
    return calls


def test_loads_dotenv_locally(load_calls: list[int]) -> None:
    assert auth._load_dotenv_if_local() is True
    assert load_calls == [1]


def test_skips_dotenv_in_a_cluster(load_calls: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "10.43.0.1")
    assert auth._load_dotenv_if_local() is False
    assert load_calls == []


@pytest.mark.parametrize("value", ["1", "true", "YES", " on "])
def test_explicit_opt_out_skips_dotenv(load_calls: list[int], monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("MCTL_AGENTS_SKIP_DOTENV", value)
    assert auth._load_dotenv_if_local() is False
    assert load_calls == []


def test_falsey_opt_out_value_still_loads(load_calls: list[int], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCTL_AGENTS_SKIP_DOTENV", "0")
    assert auth._load_dotenv_if_local() is True
