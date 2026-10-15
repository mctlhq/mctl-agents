"""The DevLoop's own WorkItem and `we_` (mctlhq/mctl-agents#636).

`creation` classifies mctl-api's answer to `POST /api/v1/work-items`;
`ensure_issue_execution` resolves the item, attaches this workflow's run, and
never answers a refusal with a second item or a local identity.
"""
from __future__ import annotations

import json
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment

from orchestrator.temporal.activities import canonical_execution as ce
from orchestrator.work_context import creation
from orchestrator.work_context import executions as ex
from orchestrator.work_context.client import WorkItemClient, WorkItemUnavailable, _HTTPResult
from orchestrator.work_context.contract import (
    WORK_ITEM_ABSENT,
    WORK_ITEM_FOUND,
    WORK_ITEM_UNKNOWN,
    WorkItem,
    WorkItemAnswer,
)

pytestmark = pytest.mark.anyio

URL = "https://github.com/mctlhq/mctl-telegram/issues/7"
KEY = "https://github.com/mctlhq/mctl-telegram/issues/7"
WI = "wi_11111111-1111-4111-8111-111111111111"
RUN = "dev-loop-mctlhq-mctl-telegram-7#run_r1"


def _item(**over: Any) -> dict[str, Any]:
    return {"schema_version": "workitem/v1", "work_item": {
        "id": WI, "tenant": "mctl", "external_key": KEY, "state": "active", **over}}


# -- creation ----------------------------------------------------------------


def test_the_body_carries_no_identity_and_a_title_independent_of_the_issue_text():
    body = creation.create_body("mctl", URL, RUN)

    assert body["external_key"] == KEY and body["tenant"] == "mctl"
    assert body["idempotency_key"] == f"devloop:github:mctlhq/mctl-telegram#7@{RUN}"
    assert not {"actor", "approver", "created_by", "principal", "subject", "email"} & set(body)
    assert creation.create_body("mctl", URL, RUN) == body  # a retry sends the same bytes


def test_another_run_of_the_issue_is_another_create_key():
    """Review on #651 (P1): mctl-api keeps create keys forever, so an
    issue-wide key would answer every later run with the first run's item,
    terminal or not. The open-work dedupe on `(tenant, external_key)` still
    joins the item while it is open."""
    one = creation.create_body("mctl", URL, RUN)
    two = creation.create_body("mctl", URL, RUN.replace("r1", "r2"))

    assert one["idempotency_key"] != two["idempotency_key"]
    assert {k: v for k, v in one.items() if k != "idempotency_key"} == {
        k: v for k, v in two.items() if k != "idempotency_key"
    }


def test_a_create_without_a_run_is_refused():
    with pytest.raises(ValueError):
        creation.create_body("mctl", URL, "")


def test_a_url_that_does_not_parse_is_an_error_not_a_guess():
    with pytest.raises(ValueError):
        creation.canonical_issue_url("https://example.com/not/an/issue")


@pytest.mark.parametrize(("status", "verdict"), [(201, creation.CREATED), (200, creation.EXISTING)])
def test_a_2xx_that_describes_our_open_item_is_usable(status, verdict):
    answer = creation.answer_from_create(status, _item(), tenant="mctl", external_key=KEY)

    assert answer.verdict == verdict and answer.work_item_id == WI and answer.usable


@pytest.mark.parametrize(
    "payload",
    [
        _item(tenant="labs"),  # another tenant
        _item(external_key=KEY + "0"),  # another issue
        _item(id="oops"),  # not a wi_
        _item(id="wi_"),
        {"schema_version": "workitem/v0", "work_item": _item()["work_item"]},
        {},
    ],
)
def test_a_2xx_that_does_not_describe_our_open_item_is_unknown_never_adopted(payload):
    answer = creation.answer_from_create(200, payload, tenant="mctl", external_key=KEY)

    assert answer.verdict == creation.UNKNOWN and not answer.usable


@pytest.mark.parametrize("state", ["completed", "superseded", "archived"])
def test_our_item_in_a_terminal_state_is_a_refusal_that_names_it(state):
    """claude P3 on #651: definite, so a refusal (no retries), with a reason
    that says what it is."""
    answer = creation.answer_from_create(200, _item(state=state), tenant="mctl", external_key=KEY)

    assert answer.verdict == creation.REFUSED and answer.code == creation.TERMINAL_CODE
    assert answer.work_item_id == WI and state in answer.reason and not answer.usable


@pytest.mark.parametrize(
    ("status", "code", "verdict"),
    [
        (409, "external_key_in_use", creation.REFUSED),
        (409, "idempotency_key_reused", creation.REFUSED),
        (403, "tenant_forbidden", creation.REFUSED),
        (400, "secret_in_text", creation.REFUSED),
        (400, "actor_not_accepted", creation.REFUSED),
        (503, "work_items_unavailable", creation.UNKNOWN),
        (500, "", creation.UNKNOWN),
        (401, "", creation.UNKNOWN),
        (403, "", creation.UNKNOWN),
        (429, "", creation.UNKNOWN),
    ],
)
def test_refusals_and_uncertainty_are_told_apart(status, code, verdict):
    answer = creation.answer_from_create(status, {"code": code} if code else {}, tenant="mctl", external_key=KEY)

    assert answer.verdict == verdict and not answer.usable


def test_the_client_posts_the_body_and_never_sends_a_second_create(monkeypatch):
    sent: list[tuple[str, str, dict]] = []

    def request(self, method, path, payload=None):
        sent.append((method, path, payload))
        return _HTTPResult(409, {"code": "external_key_in_use"})

    monkeypatch.setattr(WorkItemClient, "_request", request)

    answer = WorkItemClient().create_work_item("mctl", URL, RUN)

    assert answer.verdict == creation.REFUSED and answer.code == "external_key_in_use"
    assert [(m, p) for m, p, _ in sent] == [("POST", "/api/v1/work-items")]


def test_the_client_reports_an_unreachable_store_as_unknown(monkeypatch):
    def request(self, method, path, payload=None):
        raise WorkItemUnavailable("down")

    monkeypatch.setattr(WorkItemClient, "_request", request)

    assert WorkItemClient().create_work_item("mctl", URL, RUN).verdict == creation.UNKNOWN


def test_the_client_makes_no_request_for_a_malformed_url(monkeypatch):
    monkeypatch.setattr(WorkItemClient, "_request", lambda *a, **k: pytest.fail("must not be called"))

    assert WorkItemClient().create_work_item("mctl", "nope", RUN).verdict == creation.REFUSED


# -- ensure_issue_execution ----------------------------------------------------


class _Client:
    def __init__(self, *, create=None, attach=None) -> None:
        self.create = create or creation.CreateAnswer(creation.CREATED, work_item_id=WI)
        self.attach = attach or ex.ExecutionAnswer(ex.EXECUTION_ATTACHED, execution_id="we_1", attempt=1)
        self.item = WorkItemAnswer(
            WORK_ITEM_FOUND, WorkItem(work_item_id=WI, state="active", external_key=KEY, tenant="mctl")
        )
        self.creates: list[tuple[str, str]] = []
        self.runs: list[str] = []
        self.gets: list[str] = []
        self.attaches: list[tuple[str, ex.EngineRun, str]] = []

    def create_work_item(self, tenant, issue_url, run):
        self.creates.append((tenant, issue_url))
        self.runs.append(run)
        return self.create

    def get(self, work_item_id):
        self.gets.append(work_item_id)
        return self.item

    def attach_execution(self, work_item_id, run, phase):
        self.attaches.append((work_item_id, run, phase))
        return self.attach


@pytest.fixture
def fake(monkeypatch) -> _Client:
    client = _Client()
    monkeypatch.setattr(ce, "_client", lambda: client)
    return client


async def test_an_ordinary_start_creates_one_item_and_attaches_its_own_run(fake):
    out = await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1"))

    assert out.ok and (out.work_item_id, out.execution_id) == (WI, "we_1")
    assert fake.creates == [("mctl", URL)] and fake.runs == ["wf-1"]
    assert fake.attaches == [(WI, ex.EngineRun("temporal", "wf-1"), "Running")]


async def test_a_retry_lands_on_the_same_item_and_execution(fake):
    fake.create = creation.CreateAnswer(creation.EXISTING, work_item_id=WI)
    fake.attach = ex.ExecutionAnswer(ex.EXECUTION_EXISTING, execution_id="we_1", attempt=1)
    env = ActivityEnvironment()

    first = await env.run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1"))
    second = await env.run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1"))

    assert first == second and first.execution_id == "we_1"


async def test_a_given_work_item_skips_creation(fake):
    out = await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1", WI))

    assert out.ok and fake.creates == [] and fake.gets == [WI] and fake.attaches[0][0] == WI


@pytest.mark.parametrize(
    "item",
    [
        WorkItem(work_item_id=WI, state="active", external_key=KEY + "0", tenant="mctl"),  # another issue
        WorkItem(work_item_id=WI, state="active", external_key=KEY, tenant="labs"),  # another tenant
        WorkItem(work_item_id=WI, state="active", external_key=KEY, tenant=""),  # no tenant read
        WorkItem(work_item_id=WI, state="active", external_key="", tenant="mctl"),  # no key read
    ],
)
async def test_a_given_work_item_of_another_issue_or_tenant_is_refused_and_nothing_attached(fake, item):
    """Review on #651 (P2): a supplied item skipped the create, and with it
    the tenant mapping and the issue match. The grant would be decided under
    another issue's or tenant's item while the flip targets this issue."""
    fake.item = WorkItemAnswer(WORK_ITEM_FOUND, item)

    out = await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1", WI))

    assert not out.ok and fake.attaches == [] and fake.creates == []


async def test_a_given_work_item_that_does_not_exist_is_refused(fake):
    fake.item = WorkItemAnswer(WORK_ITEM_ABSENT)

    out = await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1", WI))

    assert not out.ok and "does not exist" in out.reason and fake.attaches == []


async def test_an_unreadable_given_work_item_is_raised_never_trusted(fake):
    fake.item = WorkItemAnswer(WORK_ITEM_UNKNOWN, reason="HTTP 503")

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1", WI))

    assert caught.value.type == ce.UNAVAILABLE_TYPE and fake.attaches == []


@pytest.mark.parametrize("service", ["mctl-api", "portfolio", "newton-mcp-gateway", "mctl-telegram"])
async def test_an_issue_opens_its_item_where_the_telegram_surface_does(fake, service):
    """mctl-api dedupes open work per (tenant, external_key), and the
    Telegram surface opens every issue's item in `mctl`: a DevLoop item in
    any other tenant (`labs` was the first guess for labs-deployed products)
    would split one issue into two WorkItems."""
    await ActivityEnvironment().run(
        ce.ensure_issue_execution,
        ce.EnsureExecutionInput(f"https://github.com/mctlhq/{service}/issues/2", "wf-2"),
    )

    assert fake.creates[0][0] == "mctl"


async def test_an_unmapped_service_is_a_refusal_with_no_request(fake):
    out = await ActivityEnvironment().run(
        ce.ensure_issue_execution, ce.EnsureExecutionInput("https://github.com/mctlhq/unknown-svc/issues/2", "wf-2")
    )

    assert not out.ok and "no tenant" in out.reason and fake.creates == [] and fake.attaches == []


@pytest.mark.parametrize("verdict", [creation.REFUSED])
async def test_a_refused_create_is_a_value_and_nothing_is_attached(fake, verdict):
    fake.create = creation.CreateAnswer(verdict, reason="HTTP 409 external_key_in_use", code="external_key_in_use")

    out = await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1"))

    assert not out.ok and fake.attaches == [] and len(fake.creates) == 1


async def test_an_unknown_create_is_raised_for_a_bounded_retry_and_nothing_is_attached(fake):
    fake.create = creation.CreateAnswer(creation.UNKNOWN, reason="HTTP 503")

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1"))

    assert caught.value.type == ce.UNAVAILABLE_TYPE and fake.attaches == []


@pytest.mark.parametrize("verdict", [ex.EXECUTION_ACTIVE, ex.EXECUTION_REFUSED])
async def test_an_active_or_refused_attach_stops_the_start(fake, verdict):
    fake.attach = ex.ExecutionAnswer(verdict, reason="execution_active")

    out = await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1"))

    assert not out.ok and out.execution_id == ""


async def test_an_unknown_attach_is_raised_never_a_local_identity(fake):
    fake.attach = ex.ExecutionAnswer(ex.EXECUTION_UNKNOWN, reason="HTTP 503")

    with pytest.raises(ApplicationError) as caught:
        await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput(URL, "wf-1"))

    assert caught.value.type == ce.UNAVAILABLE_TYPE


async def test_a_malformed_issue_url_is_a_refusal(fake):
    out = await ActivityEnvironment().run(ce.ensure_issue_execution, ce.EnsureExecutionInput("nope", "wf-1"))

    assert not out.ok and fake.creates == []


def test_the_policy_checkpoint_allows_only_the_create_rule_for_work_items():
    from orchestrator import policy_checkpoint as pc

    decision = pc.checkpoint(
        pc.MCTL_WORK_ITEM_WRITE, creation.CREATE_OPERATION, KEY, {"tenant": "mctl"}, metadata={"tenant": "mctl"}
    )
    assert decision.permitted
    other = pc.checkpoint(pc.MCTL_WORK_ITEM_WRITE, "delete:work-item", KEY, {}, metadata={})
    assert not other.permitted
    assert json.dumps(decision.verdict)  # smoke: a plain value


def test_a_read_work_item_carries_its_tenant():
    item = WorkItem.from_payload({"id": WI, "state": "active", "state_version": 1, "tenant": "mctl"})

    assert item is not None and item.tenant == "mctl"
