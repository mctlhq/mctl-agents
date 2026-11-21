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


# Import-time wiring: the module-level call is what makes the guard effective.
# `load_dotenv` is replaced in the child before `orchestrator.auth` is imported,
# so the check does not depend on where a `.env` file would be discovered.
_CHILD = (
    "import os, dotenv\n"
    "dotenv.load_dotenv = lambda *a, **k: os.environ.__setitem__('DOTENV_SENTINEL', '1')\n"
    "import orchestrator.auth\n"
    "print(os.environ.get('DOTENV_SENTINEL', 'absent'))\n"
)


def _import_auth_in_child(extra_env: dict[str, str]) -> str:
    import os
    import subprocess
    import sys
    from pathlib import Path

    env = {k: v for k, v in os.environ.items()
           if k not in {"KUBERNETES_SERVICE_HOST", "MCTL_AGENTS_SKIP_DOTENV", "DOTENV_SENTINEL"}}
    env.update(extra_env)
    root = Path(__file__).resolve().parent.parent
    out = subprocess.run([sys.executable, "-c", _CHILD], cwd=root, env=env,
                         capture_output=True, text=True, timeout=60, check=True)
    return out.stdout.strip().splitlines()[-1]


def test_importing_auth_in_a_cluster_does_not_load_dotenv() -> None:
    assert _import_auth_in_child({"KUBERNETES_SERVICE_HOST": "10.43.0.1"}) == "absent"


def test_importing_auth_locally_loads_dotenv() -> None:
    assert _import_auth_in_child({}) == "1"
