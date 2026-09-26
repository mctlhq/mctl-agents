"""Sweep-level resilience and the CLI (orchestrator.run_usage_collector.collect / main)."""
from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from orchestrator import run_usage_collector as collector
from orchestrator.run_usage_collector import Artifact, CollectResult, collect

TOKEN = "usage-writer-token-for-tests-0123456789"
BASE_URL = "https://api.example.test"


def _now_utc() -> datetime:
    return datetime.now(UTC)


def test_one_repositorys_listing_failure_does_not_abort_the_sweep():
    def list_fn(repo, cutoff, max_pages):
        if repo == "mctlhq/broken":
            raise RuntimeError("401 Bad credentials")
        return [Artifact(id=1, created_at=_now_utc().isoformat())]

    def download_fn(repo, artifact_id, max_bytes):
        return [{"session_id": "s1", "model_key": "haiku"}]

    result = collect(
        repos=["mctlhq/broken", "mctlhq/.github"],
        dry_run=True,
        list_fn=list_fn,
        download_fn=download_fn,
    )
    assert result.repositories == 2
    assert result.failures == 1
    assert result.artifacts_found == 1
    assert result.artifacts_read == 1
    assert result.records_posted == 1


def test_one_artifacts_download_failure_does_not_abort_the_sweep():
    def list_fn(repo, cutoff, max_pages):
        return [Artifact(id=1, created_at=_now_utc().isoformat()), Artifact(id=2, created_at=_now_utc().isoformat())]

    def download_fn(repo, artifact_id, max_bytes):
        if artifact_id == 1:
            raise RuntimeError("boom")
        return [{"session_id": "s2", "model_key": "opus"}]

    result = collect(repos=["mctlhq/.github"], dry_run=True, list_fn=list_fn, download_fn=download_fn)
    assert result.artifacts_found == 2
    assert result.artifacts_read == 1
    assert result.artifacts_skipped == 1
    assert result.failures == 1
    assert result.records_posted == 1


def test_artifact_returning_none_is_skipped_but_not_a_failure():
    """A guard-triggered skip (oversize zip, wrong shape, ...) is not the
    same as a raised exception — it must count as skipped, not failed."""
    def list_fn(repo, cutoff, max_pages):
        return [Artifact(id=1, created_at=_now_utc().isoformat())]

    def download_fn(repo, artifact_id, max_bytes):
        return None

    result = collect(repos=["mctlhq/.github"], dry_run=True, list_fn=list_fn, download_fn=download_fn)
    assert result.artifacts_skipped == 1
    assert result.artifacts_read == 0
    assert result.failures == 0


def test_dry_run_posts_nothing_with_no_writer_token_present(monkeypatch):
    monkeypatch.delenv(collector.TOKEN_ENV, raising=False)

    def list_fn(repo, cutoff, max_pages):
        return [Artifact(id=1, created_at=_now_utc().isoformat())]

    def download_fn(repo, artifact_id, max_bytes):
        return [{"session_id": "s1", "model_key": "haiku"}]

    def _boom_post(url, body, headers):
        raise AssertionError("dry-run must never POST")

    result = collect(
        repos=["mctlhq/.github"], dry_run=True, token="", base_url=BASE_URL,
        post=_boom_post, list_fn=list_fn, download_fn=download_fn,
    )
    assert result.records_posted == 1
    assert result.failures == 0


def test_a_repositorys_records_are_delivered_before_a_later_repository_is_swept():
    """A wide backfill must make partial progress: delivery happens per
    repository, not once after the whole repos list has been swept, so a
    repository already processed is already posted even if a later one in
    the same tick fails or the process never gets to it."""
    def list_fn(repo, cutoff, max_pages):
        if repo == "mctlhq/broken":
            raise RuntimeError("401 Bad credentials")
        return [Artifact(id=1, created_at=_now_utc().isoformat())]

    def download_fn(repo, artifact_id, max_bytes):
        return [{"session_id": f"s-{repo}", "model_key": "haiku"}]

    posted_bodies: list[dict] = []

    def post(url, body, headers):
        posted_bodies.append(body)
        return httpx.Response(
            200, json={"accepted_count": len(body["records"]), "deduped_count": 0},
            request=httpx.Request("POST", url),
        )

    result = collect(
        repos=["mctlhq/first", "mctlhq/broken", "mctlhq/second"],
        token=TOKEN, base_url=BASE_URL, post=post,
        list_fn=list_fn, download_fn=download_fn,
    )
    # Delivered once per successful repository, each carrying only that
    # repository's own record — never buffered across the whole sweep.
    assert len(posted_bodies) == 2
    assert [body["records"][0]["session_id"] for body in posted_bodies] == [
        "s-mctlhq/first", "s-mctlhq/second",
    ]
    assert result.records_posted == 2
    assert result.failures == 1


def test_gh_authentication_failure_exits_non_zero(monkeypatch):
    def _fail(args):
        raise collector.CommandFailed(1, ["gh", *args], output="", stderr="401 Bad credentials")
    monkeypatch.setattr(collector, "_run_gh", _fail)
    monkeypatch.setattr("sys.argv", ["run_usage_collector", "--dry-run"])
    with pytest.raises(SystemExit) as exc_info:
        collector.main()
    assert exc_info.value.code != 0


def test_summary_line_names_every_counter():
    result = CollectResult(
        repositories=3, artifacts_found=4, artifacts_read=2,
        artifacts_skipped=2, records_posted=5, records_deduped=1, failures=1,
    )
    summary = result.summary()
    for field in ("repositories", "artifacts_found", "artifacts_read", "artifacts_skipped",
                  "records_posted", "records_deduped", "failures"):
        assert field in summary


def test_successful_run_exits_zero_even_with_per_item_failures(monkeypatch):
    monkeypatch.setattr(collector, "_check_gh_auth", lambda: None)
    monkeypatch.setattr(collector, "collect", lambda **kwargs: CollectResult(repositories=1, failures=3))
    monkeypatch.setattr("sys.argv", ["run_usage_collector", "--dry-run"])
    with pytest.raises(SystemExit) as exc_info:
        collector.main()
    assert exc_info.value.code == 0
