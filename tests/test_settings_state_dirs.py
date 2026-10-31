from __future__ import annotations

import pytest

from config import settings


def test_state_dir_set(monkeypatch, tmp_path):
    monkeypatch.setenv("STATE_DIR", str(tmp_path))
    assert settings.agent_state_root() == tmp_path
    assert settings.service_inbox_dir("mctl-web") == tmp_path / "mctl-web" / "inbox"
    assert settings.service_proposals_dir("mctl-web") == tmp_path / "mctl-web" / "proposals"
    assert settings.mentor_digest_dir() == tmp_path / "_mentor" / "digest"


@pytest.mark.parametrize("value", [None, ""])
def test_state_dir_unset_or_empty_matches_baked_paths(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("STATE_DIR", raising=False)
    else:
        monkeypatch.setenv("STATE_DIR", value)
    assert settings.agent_state_root() == settings.AGENTS_DIR
    assert settings.service_inbox_dir("mctl-web") == settings.AGENTS_DIR / "mctl-web" / "inbox"
    assert settings.service_proposals_dir("mctl-web") == settings.AGENTS_DIR / "mctl-web" / "proposals"
    assert settings.mentor_digest_dir() == settings.MENTOR_DIR / "digest"


def test_ensure_dir_creates(tmp_path):
    target = tmp_path / "a" / "b"
    assert settings.ensure_dir(target) == target
    assert target.is_dir()


def test_mentor_services_only_baked_scaffolds():
    got = settings.mentor_services()
    assert got
    assert all((settings.AGENTS_DIR / s).is_dir() for s in got)
    assert "mctl-telegram" not in got
