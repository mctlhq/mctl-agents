"""Tell the operator that a DevLoop waits for a decision (mctlhq/mctl-api#964).

When a canonical DevLoop parks on a pending `aar_`, this activity sends one
Telegram message with a URL button to mctl-api's decide page:

    <base>/devloop/decide/<workflow_id>?grant=<aar_>&cd=<context digest>&xd=<content digest>

The bot carries a link and nothing else. It never approves, never attests a
press, and holds no mctl credential: the decision is made on the page, under
the operator's own fresh ZITADEL sign-in, by a human admin, and only for the
grant the link names (mctl-api#571 is unchanged). A link that has gone stale
(the proposal was rewritten, the grant was decided) is refused by the page,
so the message needs no editing or expiry of its own.

Off unless both `DEVLOOP_NOTIFY_TELEGRAM_CHAT_ID` and the
`DEVLOOP_NOTIFY_TELEGRAM_BOT_TOKEN` credential are set. Off is reported as
`disabled`, never as `sent`. A value that does not match its strict pattern
is `refused` and nothing is sent: ids are checked, not cleaned. A Telegram
failure is raised for the caller's bounded retry; the loop treats a final
failure as a missed notification, never as a decision.

Privacy: the message carries the target, the issue link, the workflow id and
the binding. All of them are already on the issue or in the URL; no proposal
text, reason or transcript is copied.
"""
from __future__ import annotations

import asyncio
import html
import os
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

from temporalio import activity
from temporalio.exceptions import ApplicationError

from orchestrator.credential_files import read_credential
from orchestrator.scout.telegram import TelegramError, send_message

TOKEN_CREDENTIAL = "DEVLOOP_NOTIFY_TELEGRAM_BOT_TOKEN"  # noqa: S105 - a credential name, not a value
CHAT_ENV = "DEVLOOP_NOTIFY_TELEGRAM_CHAT_ID"
BASE_ENV = "DEVLOOP_DECIDE_BASE_URL"
DEFAULT_BASE = "https://api.mctl.ai"
UNAVAILABLE_TYPE = "DecideNotifyUnavailable"

SENT = "sent"
DISABLED = "disabled"
REFUSED = "refused"

# The same patterns mctl-api's decide page accepts (handlers_devloop_decide.go),
# so a link built here is never one the page rejects as malformed.
_WORKFLOW_RE = re.compile(r"dev-loop-[A-Za-z0-9_.-]{1,200}")
_APPROVAL_RE = re.compile(r"aar_[A-Za-z0-9_-]{1,128}")
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_CHAT_RE = re.compile(r"-?[0-9]{1,20}")
_TARGET_RE = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,200}")
_ISSUE_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}/issues/[0-9]{1,10}")


@dataclass(frozen=True)
class DecideNotifyInput:
    workflow_id: str
    approval_ref: str
    #: `decision_context.context_digest` of the grant, as mctl-api answered it.
    context_digest: str
    #: The proposal content digest the grant is bound to (its artifact_hash).
    content_digest: str
    #: `<service>/<slug>` of the approval.
    target: str
    issue_url: str


@dataclass(frozen=True)
class DecideNotifyResult:
    status: str
    #: Which value was refused, or why the tool is off. Never a value itself.
    detail: str = ""


def _base_url() -> str | None:
    base = os.environ.get(BASE_ENV, DEFAULT_BASE).strip()
    try:
        u = urlsplit(base)
    except ValueError:
        return None
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        return None
    if u.path not in ("", "/"):
        return None
    return base.rstrip("/")


def refused_field(n: DecideNotifyInput) -> str:
    """The first input that does not match its pattern, or ""."""
    checks = (
        ("workflow_id", _WORKFLOW_RE, n.workflow_id),
        ("approval_ref", _APPROVAL_RE, n.approval_ref),
        ("context_digest", _DIGEST_RE, n.context_digest),
        ("content_digest", _DIGEST_RE, n.content_digest),
        ("target", _TARGET_RE, n.target),
        ("issue_url", _ISSUE_RE, n.issue_url),
    )
    for name, pattern, value in checks:
        if not isinstance(value, str) or not pattern.fullmatch(value):
            return name
    return ""


def decide_link(base: str, n: DecideNotifyInput) -> str:
    query = urlencode({"grant": n.approval_ref, "cd": n.context_digest, "xd": n.content_digest})
    return f"{base}/devloop/decide/{n.workflow_id}?{query}"


def message_text(n: DecideNotifyInput) -> str:
    e = html.escape
    return "\n".join((
        "<b>DevLoop is waiting for your decision</b>",
        f"Target: <code>{e(n.target)}</code>",
        f"Issue: {e(n.issue_url)}",
        f"Workflow: <code>{e(n.workflow_id)}</code>",
        f"Grant: <code>{e(n.approval_ref)}</code>",
        "",
        "The button opens a sign-in. Nothing is decided until you confirm on the page.",
    ))


def markup(link: str, issue_url: str) -> dict[str, object]:
    return {"inline_keyboard": [[{"text": "Decide", "url": link}], [{"text": "Open issue", "url": issue_url}]]}


@activity.defn
async def notify_devloop_decision(n: DecideNotifyInput) -> DecideNotifyResult:
    chat = os.environ.get(CHAT_ENV, "").strip()
    token = read_credential(TOKEN_CREDENTIAL)
    if not chat or not token:
        return DecideNotifyResult(DISABLED, f"{CHAT_ENV} or {TOKEN_CREDENTIAL} is not set")
    if not _CHAT_RE.fullmatch(chat):
        return DecideNotifyResult(REFUSED, CHAT_ENV)
    base = _base_url()
    if base is None:
        return DecideNotifyResult(REFUSED, BASE_ENV)
    bad = refused_field(n)
    if bad:
        return DecideNotifyResult(REFUSED, bad)
    try:
        await asyncio.to_thread(
            send_message, token, chat, message_text(n), markup(decide_link(base, n), n.issue_url)
        )
    except TelegramError as exc:
        # TelegramError already masks the token.
        raise ApplicationError(f"decide notification not delivered: {exc}", type=UNAVAILABLE_TYPE) from None
    return DecideNotifyResult(SENT)
