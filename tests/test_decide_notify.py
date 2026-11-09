"""The decide-link notification (mctlhq/mctl-api#964)."""
from __future__ import annotations

import dataclasses
from urllib.parse import parse_qs, urlsplit

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from orchestrator import action_approvals as aa
from orchestrator.scout.telegram import TelegramError
from orchestrator.temporal.activities import decide_notify as dn

pytestmark = pytest.mark.anyio

CTX = "sha256:" + "c" * 64
CONTENT = "sha256:" + "d" * 64
TOKEN = "123456:secret-bot-token"
GOOD = dn.DecideNotifyInput(
    workflow_id="dev-loop-mctlhq-mctl-api-964",
    approval_ref="aar_0123abc",
    context_digest=CTX,
    content_digest=CONTENT,
    target="mctl-api/issue-964-decide",
    issue_url="https://github.com/mctlhq/mctl-api/issues/964",
)


@pytest.fixture
def sent(monkeypatch):
    log: list[tuple] = []

    def fake_send(token, chat, text, markup=None, reply_to=None, client=None):
        log.append((token, chat, text, markup))
        return 1

    monkeypatch.setattr(dn, "send_message", fake_send)
    monkeypatch.setenv(dn.CHAT_ENV, "-1001234")
    monkeypatch.setenv(dn.TOKEN_CREDENTIAL, TOKEN)
    monkeypatch.delenv(dn.TOKEN_CREDENTIAL + "_FILE", raising=False)
    monkeypatch.delenv(dn.BASE_ENV, raising=False)
    return log


async def run(n: dn.DecideNotifyInput) -> dn.DecideNotifyResult:
    return await ActivityEnvironment().run(dn.notify_devloop_decision, n)


async def test_sends_one_message_whose_button_names_exactly_the_grant(sent):
    assert await run(GOOD) == dn.DecideNotifyResult(dn.SENT)
    assert len(sent) == 1
    token, chat, text, markup = sent[0]
    assert (token, chat) == (TOKEN, "-1001234")
    link = markup["inline_keyboard"][0][0]["url"]
    u = urlsplit(link)
    assert (u.scheme, u.netloc, u.path) == ("https", "api.mctl.ai", "/devloop/decide/dev-loop-mctlhq-mctl-api-964")
    assert parse_qs(u.query) == {"grant": ["aar_0123abc"], "cd": [CTX], "xd": [CONTENT]}
    assert markup["inline_keyboard"][1][0]["url"] == GOOD.issue_url
    assert "aar_0123abc" in text and GOOD.target in text
    assert TOKEN not in text


@pytest.mark.parametrize("missing", ["chat", "token"])
async def test_off_without_chat_or_token_is_disabled_never_sent(sent, monkeypatch, missing):
    monkeypatch.delenv(dn.CHAT_ENV if missing == "chat" else dn.TOKEN_CREDENTIAL)
    result = await run(GOOD)
    assert result.status == dn.DISABLED
    assert sent == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("workflow_id", "other-loop-1"),
        ("workflow_id", "dev-loop-x/../../admin"),
        ("approval_ref", "aar_x\r\nX: 1"),
        ("approval_ref", "not-a-grant"),
        ("context_digest", ""),
        ("context_digest", "sha256:short"),
        ("content_digest", "md5:" + "d" * 64),
        ("target", "<b>x</b>"),
        ("issue_url", "https://evil.example/mctlhq/mctl-api/issues/1"),
        ("issue_url", "javascript:alert(1)"),
    ],
)
async def test_a_malformed_value_is_refused_and_nothing_is_sent(sent, field, value):
    result = await run(dataclasses.replace(GOOD, **{field: value}))
    assert result == dn.DecideNotifyResult(dn.REFUSED, field)
    assert sent == []


@pytest.mark.parametrize("base", ["http://api.mctl.ai", "https://u:p@api.mctl.ai", "https://api.mctl.ai/x", "https://api.mctl.ai?a=1"])
async def test_a_base_url_that_is_not_a_plain_https_origin_is_refused(sent, monkeypatch, base):
    monkeypatch.setenv(dn.BASE_ENV, base)
    assert await run(GOOD) == dn.DecideNotifyResult(dn.REFUSED, dn.BASE_ENV)
    assert sent == []


async def test_a_chat_id_that_is_not_numeric_is_refused(sent, monkeypatch):
    monkeypatch.setenv(dn.CHAT_ENV, "@channel")
    assert await run(GOOD) == dn.DecideNotifyResult(dn.REFUSED, dn.CHAT_ENV)
    assert sent == []


async def test_a_telegram_failure_is_raised_for_retry_without_the_token(sent, monkeypatch):
    def failing(token, chat, text, markup=None, reply_to=None, client=None):
        raise TelegramError("sendMessage failed: HTTP 502 ***")

    monkeypatch.setattr(dn, "send_message", failing)
    with pytest.raises(ApplicationError) as exc:
        await run(GOOD)
    assert exc.value.type == dn.UNAVAILABLE_TYPE
    assert TOKEN not in str(exc.value)


def test_the_text_is_escaped():
    text = dn.message_text(dataclasses.replace(GOOD, target="a&b/<c>"))
    assert "a&amp;b/&lt;c&gt;" in text and "<c>" not in text


def _payload(decision_context):
    body = {
        "schema_version": aa.SCHEMA_VERSION,
        "approval": {
            "id": "aar_1", "state": "pending", "intent_hash": "sha256:" + "a" * 64,
            "expires_at": "2026-11-16T00:00:00Z",
        },
    }
    if decision_context is not None:
        body["decision_context"] = decision_context
    return body


def test_the_record_carries_mctl_apis_context_digest():
    rec = aa._record_of(_payload({"context_digest": CTX}))
    assert rec is not None and rec.context_digest == CTX


@pytest.mark.parametrize("ctx", [None, {}, {"context_digest": 7}, {"context_digest": "md5:x"}, "sha256:x"])
def test_a_missing_or_malformed_context_digest_reads_as_none_never_invented(ctx):
    rec = aa._record_of(_payload(ctx))
    assert rec is not None and rec.context_digest == ""
