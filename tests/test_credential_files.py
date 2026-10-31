"""File-backed credential readers (mctl-agents#807 G2, runtime side)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from orchestrator import auth
from orchestrator import credential_files as cf
from orchestrator.github_token import refresh_github_token

IN_PROCESS = [s.name for s in cf.INVENTORY if s.klass == cf.IN_PROCESS]
DOC = Path(__file__).resolve().parent.parent / "docs" / "security" / "credential-files.md"


def _clear(monkeypatch: pytest.MonkeyPatch) -> None:
    for spec in cf.INVENTORY:
        monkeypatch.delenv(spec.name, raising=False)
        monkeypatch.delenv(spec.file_name, raising=False)


def _file(monkeypatch, tmp_path, name: str, content: bytes | str) -> Path:
    path = tmp_path / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode())
    monkeypatch.setenv(f"{name}_FILE", str(path))
    return path


@pytest.mark.parametrize("name", IN_PROCESS)
def test_reads_file_strips_and_tracks_rotation_without_touching_environ(monkeypatch, tmp_path, name):
    _clear(monkeypatch)
    path = _file(monkeypatch, tmp_path, name, "value-A\n")
    before = dict(os.environ)
    assert cf.read_credential(name) == "value-A"
    path.write_text("value-B\n")
    assert cf.read_credential(name) == "value-B"
    assert dict(os.environ) == before


def test_file_wins_over_env_and_env_is_the_fallback(monkeypatch, tmp_path):
    _clear(monkeypatch)
    monkeypatch.setenv("MCTL_TOKEN", "from-env")
    assert cf.read_credential("MCTL_TOKEN") == "from-env"
    _file(monkeypatch, tmp_path, "MCTL_TOKEN", "from-file")
    assert cf.read_credential("MCTL_TOKEN") == "from-file"
    assert cf.read_credential("MCTL_TOKEN", {"MCTL_TOKEN": "explicit"}) == "explicit"
    monkeypatch.delenv("MCTL_TOKEN")
    monkeypatch.delenv("MCTL_TOKEN_FILE")
    assert cf.read_credential("MCTL_TOKEN") == ""


@pytest.mark.parametrize("bad", [b"", b"  \n", b"abc\xff\xfe"])
def test_unreadable_empty_or_invalid_keeps_last_good(monkeypatch, tmp_path, bad):
    _clear(monkeypatch)
    path = _file(monkeypatch, tmp_path, "MCTL_TOKEN", "good")
    assert cf.read_credential("MCTL_TOKEN") == "good"
    path.write_bytes(bad)
    assert cf.read_credential("MCTL_TOKEN") == "good"
    path.unlink()
    assert cf.read_credential("MCTL_TOKEN") == "good"


def test_bad_file_without_last_good_falls_back_to_env_then_absent(monkeypatch, tmp_path):
    _clear(monkeypatch)
    _file(monkeypatch, tmp_path, "MCTL_TOKEN", b"\xff")
    assert cf.read_credential("MCTL_TOKEN") == ""
    monkeypatch.setenv("MCTL_TOKEN", "seed")
    assert cf.read_credential("MCTL_TOKEN") == "seed"


def test_failed_read_logs_only_name_path_and_exception_type(monkeypatch, tmp_path, caplog, capsys):
    _clear(monkeypatch)
    path = _file(monkeypatch, tmp_path, "MCTL_TOKEN", b"ghs_SECRETMARKER\xff\xfe")
    with caplog.at_level(logging.DEBUG):
        cf.read_credential("MCTL_TOKEN")
        refresh_github_token()
    monkeypatch.setenv("GITHUB_TOKEN_FILE", str(path))
    with caplog.at_level(logging.DEBUG):
        refresh_github_token()
    out = capsys.readouterr()
    text = caplog.text + out.out + out.err
    assert "SECRETMARKER" not in text
    assert "0xff" not in text and "byte" not in text
    assert "MCTL_TOKEN" in text and str(path) in text and "UnicodeDecodeError" in text


def test_github_token_refresh_authenticates_from_file_only(monkeypatch, tmp_path):
    _clear(monkeypatch)
    path = _file(monkeypatch, tmp_path, "GITHUB_TOKEN", "ghs_A")
    refresh_github_token()
    assert os.environ["GITHUB_TOKEN"] == "ghs_A"
    path.write_text("ghs_B")
    refresh_github_token()
    assert os.environ["GITHUB_TOKEN"] == "ghs_B"


def test_rest_client_reads_the_file_per_request(monkeypatch, tmp_path):
    from orchestrator.lifecycle import client as lc

    _clear(monkeypatch)
    path = _file(monkeypatch, tmp_path, "MCTL_TOKEN", "A")
    seen: list[str] = []

    class _Opener:
        def open(self, req, timeout=None):
            seen.append(req.get_header("Authorization"))
            raise OSError("stop")

    c = lc.OwnershipClient(base_url="https://api.example.test")
    c._opener = _Opener()
    before = dict(os.environ)
    for value in ("A", "B"):
        path.write_text(value)
        with pytest.raises(Exception):  # noqa: B017 — transport stub aborts the call
            c._request("GET", "/x")
    assert seen == ["Bearer A", "Bearer B"]
    assert dict(os.environ) == before


def test_usage_recorder_and_evidence_writer_use_the_file(monkeypatch, tmp_path):
    from orchestrator import evidence_producer, usage_ledger

    _clear(monkeypatch)
    ev = _file(monkeypatch, tmp_path, "MCTL_EVIDENCE_WRITER_TOKEN", "E1")
    us = _file(monkeypatch, tmp_path, "MCTL_USAGE_WRITER_TOKEN", "U1")
    assert evidence_producer.writer_credentials(os.environ)[0] == "E1"
    ev.write_text("E2")
    assert evidence_producer.writer_credentials(os.environ)[0] == "E2"

    sent: list[str] = []

    def post(url, body, headers):
        sent.append(headers["Authorization"])
        raise RuntimeError("stop")

    rec = usage_ledger.UsageRecorder.from_env("investigator")
    rec._post = post
    rec._sleep = lambda _s: None
    assert rec.enabled
    us.write_text("U2")
    try:
        rec._deliver([{"x": 1}])
    except Exception:  # noqa: S110, BLE001 — only the sent header matters
        pass
    assert sent and sent[0] == "Bearer U2"


def test_scrubbed_blanks_file_pointers_and_adds_one_active_credential(monkeypatch, tmp_path):
    from orchestrator import options

    _clear(monkeypatch)
    for name in IN_PROCESS:
        _file(monkeypatch, tmp_path, name, f"secret-{name}")
    _file(monkeypatch, tmp_path, "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-file")
    _file(monkeypatch, tmp_path, "ANTHROPIC_API_KEY", "sk-ant-api03-file")
    before = dict(os.environ)
    opts = options._scrubbed(options.ClaudeAgentOptions())
    for name in cf.file_env_names():
        assert opts.env[name] == ""
    assert not any(f"secret-{n}" in v for n in IN_PROCESS for v in opts.env.values())
    assert opts.env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-file"
    assert opts.env.get("ANTHROPIC_API_KEY", "") == os.environ.get("ANTHROPIC_API_KEY", "")
    assert dict(os.environ) == before


def test_429_rotation_switches_to_secondary_file_without_environ_write(monkeypatch, tmp_path):
    _clear(monkeypatch)
    _file(monkeypatch, tmp_path, "CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-primary")
    _file(monkeypatch, tmp_path, "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "sk-ant-oat01-secondary")
    before = dict(os.environ)
    assert auth.session_auth_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-primary"}
    assert auth.rotate_to_secondary_auth() is True
    assert auth.session_auth_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-secondary"}
    assert dict(os.environ) == before


def test_rotation_is_a_noop_without_a_secondary(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-primary")
    assert auth.rotate_to_secondary_auth() is False
    assert auth.session_auth_env() == {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-primary"}


def test_residual_boundary_and_inventory_doc_stay_in_sync():
    assert "#869" in cf.RESIDUAL_BOUNDARY
    text = DOC.read_text()
    assert cf.RESIDUAL_BOUNDARY in text
    documented = set()
    for line in text.splitlines():
        if line.startswith("| `"):
            documented.add(line.split("`")[1])
    assert documented == {s.name for s in cf.INVENTORY}
