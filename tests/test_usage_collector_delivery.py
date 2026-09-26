"""Delivery (orchestrator.run_usage_collector.deliver)."""
from __future__ import annotations

import json

import httpx

from orchestrator.run_usage_collector import DeliveryResult, deliver
from orchestrator.usage_ledger import BASE_URL_ENV, TOKEN_ENV

TOKEN = "usage-writer-token-for-tests-0123456789"
BASE_URL = "https://api.example.test"


def _record(session_id: str, model_key: str = "haiku", **extra) -> dict:
    return {"schema_version": 1, "session_id": session_id, "model_key": model_key, **extra}


class FakePoster:
    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls: list[tuple[str, dict, dict]] = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        answer = self.answers.pop(0) if self.answers else httpx.Response(
            200, json={"accepted_count": len(body["records"]), "deduped_count": 0},
            request=httpx.Request("POST", url),
        )
        if isinstance(answer, Exception):
            raise answer
        return answer


def _ok(accepted: int, deduped: int = 0) -> httpx.Response:
    return httpx.Response(
        200, json={"accepted_count": accepted, "deduped_count": deduped},
        request=httpx.Request("POST", "https://api.example.test/api/v1/usage/records"),
    )


def test_authorization_header_carries_only_the_writer_token():
    poster = FakePoster()
    records = [_record("s1")]
    deliver(records, token=TOKEN, base_url=BASE_URL, post=poster)
    _, _, headers = poster.calls[0]
    assert headers == {"Authorization": f"Bearer {TOKEN}"}
    assert "MCTL_TOKEN" not in json.dumps(headers)


def test_chunks_at_the_record_cap():
    poster = FakePoster()
    records = [_record(f"s{i}") for i in range(1201)]
    result = deliver(records, token=TOKEN, base_url=BASE_URL, post=poster, max_batch=500, max_body_bytes=1 << 30)
    sizes = [len(body["records"]) for _, body, _ in poster.calls]
    assert sizes == [500, 500, 201]
    assert result.accepted == 1201


def test_chunks_at_the_byte_cap():
    poster = FakePoster()
    # Each record serialises to well over 100 bytes; a tiny body cap forces
    # a new chunk every record or two.
    records = [_record(f"s{i}", extra_padding="x" * 200) for i in range(5)]
    result = deliver(records, token=TOKEN, base_url=BASE_URL, post=poster, max_batch=500, max_body_bytes=300)
    assert len(poster.calls) > 1
    for _, body, _ in poster.calls:
        assert len(json.dumps(body).encode("utf-8")) <= 300 or len(body["records"]) == 1
    assert result.accepted == 5


def test_non_https_base_url_sends_nothing(capsys):
    poster = FakePoster()
    result = deliver([_record("s1")], token=TOKEN, base_url="http://api.example.test", post=poster)
    assert poster.calls == []
    assert result.undelivered == 1
    assert result.failed_chunks == 0
    assert BASE_URL_ENV in capsys.readouterr().out


def test_absent_token_warns_once_and_posts_nothing(capsys):
    poster = FakePoster()
    result = deliver([_record("s1"), _record("s2")], token="", base_url=BASE_URL, post=poster)
    assert poster.calls == []
    assert result.undelivered == 2
    assert result.failed_chunks == 0
    assert TOKEN_ENV in capsys.readouterr().out


def test_no_records_is_a_no_op():
    poster = FakePoster()
    result = deliver([], token=TOKEN, base_url=BASE_URL, post=poster)
    assert poster.calls == []
    assert result == DeliveryResult()


def test_a_failed_chunk_does_not_stop_the_next(capsys):
    bad_response = httpx.Response(
        400, json={"error": "bad batch"},
        request=httpx.Request("POST", "https://api.example.test/api/v1/usage/records"),
    )
    poster = FakePoster(bad_response)  # first chunk: HTTP 400; second falls through to the default 200
    records = [_record("bad"), _record("good")]
    result = deliver(records, token=TOKEN, base_url=BASE_URL, post=poster, max_batch=1, max_body_bytes=1 << 20)
    assert len(poster.calls) == 2
    assert result.undelivered == 1
    assert result.failed_chunks == 1
    assert result.accepted == 1
    assert "400" in capsys.readouterr().out


def test_a_transport_error_on_one_chunk_does_not_stop_the_next():
    poster = FakePoster(httpx.ConnectError("boom"))
    records = [_record("bad"), _record("good")]
    result = deliver(records, token=TOKEN, base_url=BASE_URL, post=poster, max_batch=1, max_body_bytes=1 << 20)
    assert len(poster.calls) == 2
    assert result.undelivered == 1
    assert result.failed_chunks == 1
    assert result.accepted == 1


def test_accepted_and_deduped_counts_are_surfaced():
    poster = FakePoster(_ok(0, deduped=2))
    result = deliver([_record("s1"), _record("s2")], token=TOKEN, base_url=BASE_URL, post=poster,
                      max_batch=500, max_body_bytes=1 << 20)
    assert result.accepted == 0
    assert result.deduped == 2
