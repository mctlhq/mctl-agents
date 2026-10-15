"""The DevLoop's own WorkItem: resolve-or-create it for an issue
(mctlhq/mctl-agents#636, mctl-api#571 design section 0).

`POST /api/v1/work-items` is the idempotency and resolution boundary. It
creates the item, or answers the open item that already has the same
`(tenant, external_key)`, so asking is also resolving; no other
issue-to-work-item lookup exists or is invented here. This module is the
client side of that rule, stdlib-only like the rest of
`orchestrator.work_context`:

- `canonical_issue_url` is the byte-exact `external_key`. mctl-api compares
  it byte for byte, so `Mctlhq/...` or `/issues/0571` would otherwise open a
  second item for the same issue.
- `create_body` carries no identity field (mctl-api refuses any with
  `actor_not_accepted`) and a title that does not depend on the issue, so a
  retry sends the same bytes: the idempotency key covers the whole body.
- `answer_from_create` classifies the answer. A 2xx counts only when it
  describes exactly this tenant and external key and an open item.
  `external_key_in_use` and every other refusal is a value the caller turns
  into a failed start; none of them is permission to create another item.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator.temporal.issue_ref import parse_issue_url
from orchestrator.work_context.contract import SCHEMA_VERSION, TERMINAL_WORK_ITEM_STATES, WORK_ITEM_STATES

CREATE_OPERATION = "create:work-item"

#: mctl-api's `workitems` id prefix for a work item.
WORK_ITEM_ID_PREFIX = "wi_"

CREATED = "work-item-created"
EXISTING = "work-item-existing"
#: A definite no from mctl-api (typed 4xx, or the policy checkpoint): the
#: start must stop, and a retry would be refused the same way.
REFUSED = "work-item-refused"
#: No usable answer (transport, 5xx, 401/403/408/429, a malformed body).
UNKNOWN = "work-item-unknown"
#: Our own `code` (not mctl-api's) for an answer naming this issue's item in
#: a terminal state.
TERMINAL_CODE = "work_item_terminal"

#: 4xx statuses that say nothing about the request itself.
_UNCERTAIN_4XX = frozenset({401, 403, 408, 425, 429})


@dataclass(frozen=True)
class CreateAnswer:
    verdict: str
    work_item_id: str = ""
    reason: str = ""
    #: mctl-api's typed `code`, when it sent one. Never free text from the body.
    code: str = ""

    @property
    def usable(self) -> bool:
        return self.verdict in (CREATED, EXISTING)


def canonical_issue_url(issue_url: str) -> str:
    """`https://github.com/<owner>/<repo>/issues/<number>`, owner and repo
    lowercased, the number in decimal without leading zeros, from the same
    parse that names the workflow. Raises ValueError for a URL that does not
    parse: the run must stop, not guess."""
    parts = parse_issue_url(issue_url)
    return f"https://github.com/{parts.owner.lower()}/{parts.repo.lower()}/issues/{int(parts.number)}"


def idempotency_key_for(issue_url: str, run: str) -> str:
    """`devloop:github:<owner>/<repo>#<number>@<run>`, normalised like the
    URL. Scoped to the run: a retry of one run replays its own create, while
    mctl-api keeps keys forever, so an issue-wide key would answer every
    later run with the first run's item even after that item ended (a run
    after it must open the issue's next item; the open-work dedupe on
    `(tenant, external_key)` still joins an item that is open)."""
    if not run:
        raise ValueError("a work item create needs the run it is for")
    parts = parse_issue_url(issue_url)
    return f"devloop:github:{parts.owner.lower()}/{parts.repo.lower()}#{int(parts.number)}@{run}"


def create_body(tenant: str, issue_url: str, run: str) -> dict[str, Any]:
    parts = parse_issue_url(issue_url)
    return {
        "tenant": tenant,
        "title": f"DevLoop {parts.owner.lower()}/{parts.repo.lower()}#{int(parts.number)}",
        "external_key": canonical_issue_url(issue_url),
        "idempotency_key": idempotency_key_for(issue_url, run),
        "visibility": "tenant",
        "origin_surface": "api",
    }


def answer_from_create(status: int, payload: dict[str, Any], *, tenant: str, external_key: str) -> CreateAnswer:
    """Classify mctl-api's answer to `create_body`. 201 created, 200 the
    existing open item (an idempotent replay or an `external_key` dedupe)."""
    raw_code = payload.get("code")
    code: str = raw_code if isinstance(raw_code, str) else ""
    if status in (200, 201):
        item = payload.get("work_item") if payload.get("schema_version") == SCHEMA_VERSION else None
        item = item if isinstance(item, dict) else {}
        item_id, state = item.get("id"), item.get("state")
        describes_ours = (
            isinstance(item_id, str)
            and item_id.startswith(WORK_ITEM_ID_PREFIX)
            and len(item_id) > len(WORK_ITEM_ID_PREFIX)
            and item.get("tenant") == tenant
            and item.get("external_key") == external_key
            and isinstance(state, str)
            and state in WORK_ITEM_STATES
        )
        if describes_ours and state in TERMINAL_WORK_ITEM_STATES:
            # This issue's item, definitely closed (an idempotent replay
            # answers the stored item whatever its state): a retry would get
            # the same answer, so it is a refusal that names it, not unknown.
            return CreateAnswer(
                REFUSED, work_item_id=str(item_id), reason=f"work item {item_id} is {state}", code=TERMINAL_CODE
            )
        if not describes_ours:
            return CreateAnswer(UNKNOWN, reason=f"HTTP {status} does not describe an open work item for {external_key}")
        return CreateAnswer(CREATED if status == 201 else EXISTING, work_item_id=str(item_id))
    reason = f"HTTP {status} {code}".strip()
    if status == 403 and code == "tenant_forbidden":
        # Typed: this principal has no access to that tenant. A definite no.
        return CreateAnswer(REFUSED, reason=reason, code=code)
    if status >= 500 or status < 400 or status in _UNCERTAIN_4XX:
        return CreateAnswer(UNKNOWN, reason=reason, code=code)
    return CreateAnswer(REFUSED, reason=reason, code=code)
