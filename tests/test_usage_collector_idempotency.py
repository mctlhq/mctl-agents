"""The acceptance test: re-collecting the same artifact must be free.

Runs the collector twice (then a third time simulating a `--lookback-days
90` backfill) over the same faked artifact, against a fake ingest endpoint
that implements mctl-api's own dedupe rule: derive `sha256` over
length-prefixed `(session_id, result_uuid, model_key)` — or
`(session_id, model_key, num_turns)` when `result_uuid` is absent —
insert-or-ignore, and answer `accepted_count`/`deduped_count`.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

import httpx

from orchestrator.run_usage_collector import Artifact, collect

TOKEN = "usage-writer-token-for-tests-0123456789"
BASE_URL = "https://api.example.test"
FIXTURE = Path(__file__).parent / "fixtures" / "model-usage-records.json"
ARTIFACT_ID = 10894371011


def _derive_id(record: dict) -> str:
    """mctl-api's DeterministicID: sha256 of length-prefixed identity
    fields, falling back to (session_id, model_key, num_turns) when
    result_uuid is absent."""
    session_id = str(record.get("session_id", ""))
    model_key = str(record.get("model_key", ""))
    result_uuid = record.get("result_uuid")
    parts = (
        [session_id, str(result_uuid), model_key]
        if result_uuid
        else [session_id, model_key, str(record.get("num_turns", ""))]
    )
    payload = b"".join(f"{len(part)}:{part}".encode() for part in parts)
    return hashlib.sha256(payload).hexdigest()


class FakeMctlApi:
    """A minimal stand-in for mctl-api's usage ingest: insert-or-ignore on
    the derived id, answering accepted_count/deduped_count like the real
    endpoint does."""

    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.calls: list[dict] = []

    def __call__(self, url: str, body: dict, headers: dict) -> httpx.Response:
        self.calls.append(body)
        accepted = 0
        deduped = 0
        for record in body["records"]:
            row_id = _derive_id(record)
            if row_id in self.rows:
                deduped += 1
            else:
                self.rows[row_id] = record
                accepted += 1
        return httpx.Response(
            200, json={"accepted_count": accepted, "deduped_count": deduped}, request=httpx.Request("POST", url)
        )


def _list_fn(repo, cutoff, max_pages):
    return [Artifact(id=ARTIFACT_ID, created_at="2026-09-26T14:07:22Z")]


def _download_fn(repo, artifact_id, max_bytes):
    return json.loads(FIXTURE.read_text())["records"]


def test_second_collection_of_the_same_artifact_deduplicates_completely():
    api = FakeMctlApi()

    first = collect(
        repos=["mctlhq/.github"], token=TOKEN, base_url=BASE_URL, post=api,
        list_fn=_list_fn, download_fn=_download_fn,
    )
    assert first.records_posted == 2
    assert first.records_deduped == 0
    # Exactly two rows: the Haiku and Opus records of one review, not one
    # merged row.
    assert len(api.rows) == 2
    first_body = api.calls[-1]

    second = collect(
        repos=["mctlhq/.github"], token=TOKEN, base_url=BASE_URL, post=api,
        list_fn=_list_fn, download_fn=_download_fn,
    )
    second_body = api.calls[-1]

    assert second_body == first_body, "re-collecting the same artifact must post a byte-identical body"
    assert second.records_posted == 0
    assert second.records_deduped == 2
    assert len(api.rows) == 2, "a re-collection must add no row"


def test_a_90_day_backfill_after_two_prior_collections_adds_no_row():
    api = FakeMctlApi()
    results = [
        collect(
            repos=["mctlhq/.github"], lookback_days=lookback, token=TOKEN, base_url=BASE_URL, post=api,
            list_fn=_list_fn, download_fn=_download_fn,
        )
        for lookback in (3, 3, 90)
    ]

    assert results[0].records_deduped == 0
    assert results[1].records_deduped == 2
    assert results[2].records_deduped == 2
    assert len(api.rows) == 2
