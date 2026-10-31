"""STATE_DIR resolution for the mentor (mctl-agents#947)."""
from __future__ import annotations

import anyio

from config.settings import MENTOR_DIR, mentor_services
from orchestrator import run_mentor as rm
from tests.conftest import fake_mcp_client_factory
from tests.test_run_mentor import _stub_build_options


def _capture_rotation(monkeypatch):
    rotated = []
    _stub_build_options(monkeypatch, mcp_servers=None)
    monkeypatch.setattr(rm, "_rotate_old_digests", lambda d, *a, **k: rotated.append(d))
    monkeypatch.setattr(rm, "ClaudeSDKClient", fake_mcp_client_factory(statuses=[]))
    return rotated


def test_state_dir_drives_rotation_and_prompt(monkeypatch, tmp_path):
    state = tmp_path / "state"
    (state / "mctl-telegram" / "proposals").mkdir(parents=True)
    monkeypatch.setenv("STATE_DIR", str(state))
    rotated = _capture_rotation(monkeypatch)
    anyio.run(rm.run_mentor)
    assert rotated == [state / "_mentor" / "digest"]
    assert (state / "_mentor" / "digest").is_dir()

    prompt = rm.build_prompt()
    assert str(state / "_mentor" / "digest") in prompt
    for s in mentor_services():
        assert f"`{state / s / 'proposals'}`" in prompt
    assert str(state / "mctl-telegram") not in prompt


def test_unset_state_dir_uses_baked_digest(monkeypatch):
    monkeypatch.delenv("STATE_DIR", raising=False)
    rotated = _capture_rotation(monkeypatch)
    monkeypatch.setattr(rm, "ensure_dir", lambda p: p)
    anyio.run(rm.run_mentor)
    assert rotated == [MENTOR_DIR / "digest"]
