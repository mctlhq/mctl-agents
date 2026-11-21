"""orchestrator.temporal.mctl_client: token resolution and auth headers (#201)."""
from __future__ import annotations

import pytest

from orchestrator.temporal import mctl_client


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCTL_TOKEN", raising=False)
    monkeypatch.delenv("MCTL_TOKEN_FILE", raising=False)


def test_token_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCTL_TOKEN", "env-token")
    assert mctl_client.mctl_token() == "env-token"


def test_token_file_wins_over_env(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    f = tmp_path / "tok"
    f.write_text("file-token\n")
    monkeypatch.setenv("MCTL_TOKEN_FILE", str(f))
    monkeypatch.setenv("MCTL_TOKEN", "env-token")
    assert mctl_client.mctl_token() == "file-token"


def test_missing_token_raises_not_empty_bearer() -> None:
    with pytest.raises(RuntimeError, match="MCTL_TOKEN is not set"):
        mctl_client.mctl_token()
    with pytest.raises(RuntimeError):
        mctl_client.auth_headers()


def test_auth_headers_are_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCTL_TOKEN", "abc")
    assert mctl_client.auth_headers() == {"Authorization": "Bearer abc"}
