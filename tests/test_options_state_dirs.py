"""add_dirs carry the STATE_DIR paths; cwd is unchanged (mctl-agents#947)."""
from __future__ import annotations

from orchestrator import options


def test_service_agent_and_mentor_add_dirs_include_state_dirs(tmp_path, monkeypatch):
    monkeypatch.setenv("STATE_DIR", str(tmp_path / "state"))
    service_dir = tmp_path / "agents" / "mctl-web"
    service_dir.mkdir(parents=True)
    inbox = tmp_path / "state" / "mctl-web" / "inbox"
    props = tmp_path / "state" / "mctl-web" / "proposals"
    built = options.build_service_agent_options(service_dir, "m", state_dirs=[inbox, props])
    assert str(inbox) in built.add_dirs and str(props) in built.add_dirs
    assert built.cwd == str(service_dir)

    mentor_dir = tmp_path / "agents" / "_mentor"
    mentor_dir.mkdir()
    m = options.build_mentor_options(mentor_dir, "m")
    assert str(tmp_path / "state") in m.add_dirs
    assert m.cwd == str(mentor_dir.parent)
