"""Bounded, binary-safe download (orchestrator.run_usage_collector.download_records).

Builds real zips with `zipfile` and fakes only the `gh api …/zip` subprocess
call, so the size guards and the single-member read are exercised against
genuine zip bytes rather than a mock of the zip format itself.
"""
from __future__ import annotations

import os
import subprocess
import zipfile

import pytest

from orchestrator import run_usage_collector as collector


def _zip_writer(member_name: str, payload: bytes):
    """A stand-in for `subprocess.run(["gh", "api", ...], stdout=handle)`
    that instead writes a real zip containing one member to whatever file
    handle it's given — `zipfile.ZipFile` writes directly to a seekable
    binary file object, no path needed."""
    def _fake_run(cmd, *, stdout, check, timeout):
        with zipfile.ZipFile(stdout, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(member_name, payload)
        return subprocess.CompletedProcess(cmd, 0)
    return _fake_run


@pytest.fixture(autouse=True)
def _no_refresh(monkeypatch):
    monkeypatch.setattr(collector, "refresh_github_token", lambda: None)


def _track_temp_files(monkeypatch):
    created: list[str] = []
    real_mkstemp = collector.tempfile.mkstemp

    def _spy(*args, **kwargs):
        fd, path = real_mkstemp(*args, **kwargs)
        created.append(path)
        return fd, path
    monkeypatch.setattr(collector.tempfile, "mkstemp", _spy)
    return created


def test_happy_path_reads_the_member(monkeypatch, capsys):
    payload = b'{"records": [{"session_id": "s1", "model_key": "haiku"}]}'
    monkeypatch.setattr(collector.subprocess, "run", _zip_writer(collector.ARTIFACT_MEMBER, payload))
    created = _track_temp_files(monkeypatch)

    records = collector.download_records("mctlhq/.github", 1)

    assert records == [{"session_id": "s1", "model_key": "haiku"}]
    assert len(created) == 1
    assert not os.path.exists(created[0]), "temp file must be removed after reading"


def test_oversize_zip_is_refused_before_read(monkeypatch, capsys):
    payload = b"x" * 200
    monkeypatch.setattr(collector.subprocess, "run", _zip_writer(collector.ARTIFACT_MEMBER, payload))
    created = _track_temp_files(monkeypatch)

    records = collector.download_records("mctlhq/.github", 1, max_bytes=10)

    assert records is None
    assert "bytes" in capsys.readouterr().out
    assert not os.path.exists(created[0])


def test_member_declaring_oversize_uncompressed_size_is_refused(monkeypatch, capsys):
    # A highly-compressible payload: the zip file itself stays tiny, but the
    # member's declared (uncompressed) size is what the guard must catch.
    payload = b"a" * 5_000_000
    monkeypatch.setattr(collector.subprocess, "run", _zip_writer(collector.ARTIFACT_MEMBER, payload))
    created = _track_temp_files(monkeypatch)

    records = collector.download_records("mctlhq/.github", 1, max_bytes=collector.MAX_ARTIFACT_BYTES)

    assert records is None
    assert "uncompressed" in capsys.readouterr().out
    assert not os.path.exists(created[0])


def test_zip_with_no_matching_member_is_skipped(monkeypatch, capsys):
    monkeypatch.setattr(collector.subprocess, "run", _zip_writer("some-other-file.json", b"{}"))
    created = _track_temp_files(monkeypatch)

    records = collector.download_records("mctlhq/.github", 1)

    assert records is None
    assert collector.ARTIFACT_MEMBER in capsys.readouterr().out
    assert not os.path.exists(created[0])


def test_non_zip_payload_is_skipped(monkeypatch, capsys):
    def _fake_run(cmd, *, stdout, check, timeout):
        stdout.write(b"not a zip file at all")
        return subprocess.CompletedProcess(cmd, 0)
    monkeypatch.setattr(collector.subprocess, "run", _fake_run)
    created = _track_temp_files(monkeypatch)

    records = collector.download_records("mctlhq/.github", 1)

    assert records is None
    assert not os.path.exists(created[0])


def test_download_failure_is_skipped_and_temp_file_removed(monkeypatch, capsys):
    def _fake_run(cmd, *, stdout, check, timeout):
        raise subprocess.CalledProcessError(1, cmd, stderr="404 Not Found")
    monkeypatch.setattr(collector.subprocess, "run", _fake_run)
    created = _track_temp_files(monkeypatch)

    records = collector.download_records("mctlhq/.github", 1)

    assert records is None
    assert not os.path.exists(created[0])


def test_non_object_records_list_entries_are_dropped(monkeypatch):
    payload = b'{"records": [{"session_id": "s1"}, "not-an-object", 42]}'
    monkeypatch.setattr(collector.subprocess, "run", _zip_writer(collector.ARTIFACT_MEMBER, payload))
    records = collector.download_records("mctlhq/.github", 1)
    assert records == [{"session_id": "s1"}]
