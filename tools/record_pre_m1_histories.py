# ruff: noqa: S101, S607  (a pytest file run inside an a99c888 checkout; see below)
"""Record the pre-M1 history fixtures for the B4 gate (mctlhq/mctl-agents#1007, P-5).

NOT a test of this tree and NOT run by CI here. It is the recorder that
produced `tests/fixtures/histories/pre_m1/*.json`, committed so the fixtures
can be audited and reproduced. It must run against the PRE-M1 code, i.e. a
checkout of a99c888 (the 1.117.0 release line, before mctlhq/mctl-agents#998
added `workflow.patched("deferred-resume-contract")`):

    git worktree add --detach ../agents-p5-base a99c888
    cp tools/record_pre_m1_histories.py ../agents-p5-base/tests/test_zz_record_pre_m1.py
    cd ../agents-p5-base
    PRE_M1_OUT=<this tree>/tests/fixtures/histories/pre_m1 python -m pytest -q tests/test_zz_record_pre_m1.py

It is a pytest file so it can reuse a99c888's own fakes and fixtures (the
fake mctl-api, the action-approval store) exactly as that release's tests
drove them. Every scenario writes one history per workflow RUN.

Never re-record these files from a post-M1 checkout: their value is that
they were produced by code that had no M1 marker.
`tests/test_pre_m1_compat.py` asserts that no fixture carries the marker, so
an accidental re-record fails loudly instead of passing vacuously.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import uuid
from pathlib import Path
from typing import Any

import anyio
import pytest
from temporalio.common import RetryPolicy
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner

pytestmark = pytest.mark.anyio

OUT = Path(os.environ.get("PRE_M1_OUT", "")) if os.environ.get("PRE_M1_OUT") else None


def _scrub(obj: Any) -> None:
    """Blank failure stack traces and worker identities: they carry the
    recording machine's absolute paths and pid@hostname, and replay reads
    neither."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("stackTrace", "identity") and value:
                obj[key] = ""
            else:
                _scrub(value)
    elif isinstance(obj, list):
        for value in obj:
            _scrub(value)


def _canonical_sha256(history: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(history, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _write(name: str, history: dict[str, Any], workflow_id: str | None) -> None:
    """Write one history and register it in MANIFEST.json: its canonical
    sha256 (what `test_pre_m1_compat.py` checks every fixture against) and
    the workflow id it ran under (replay needs it to reproduce the activity
    arguments that embed it)."""
    assert OUT is not None, "set PRE_M1_OUT"
    _scrub(history)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"{name}.json").write_text(json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    manifest_path = OUT / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.setdefault("files", {})[f"{name}.json"] = {
        "sha256": _canonical_sha256(history),
        "workflow_id": workflow_id,
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture
async def env():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


def test_manifest():
    sha = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()
    import temporalio

    assert OUT is not None
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "MANIFEST.json").write_text(
        json.dumps({"recorded_from": sha, "temporalio": temporalio.__version__, "files": {}}, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )


# -- DevLoopWorkflow -----------------------------------------------------------


async def test_dev_loop_full(env):
    """investigate -> approve -> (gates pass: the M1 marker's position) ->
    implement -> merge watch -> merged, in one run."""
    from tests.replay_scenarios import record, scenario_by_name

    handle = await record(env.client, scenario_by_name("dev_loop_full"))
    _write("dev_loop_full", (await handle.fetch_history()).to_json_dict(), handle.id)


async def test_dev_loop_hop_chain(env, monkeypatch):
    """The same loop with the merge watch forced to continue-as-new: run 1
    passes the gates and hops; every later run starts from a pre-M1
    `MergeWatchResume` (no `deferred_resume_contract`)."""
    from orchestrator.temporal.activities.pr_state import PRState
    from orchestrator.temporal.workflows import dev_loop
    from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef
    from tests.temporal_harness import Worker
    from tests.test_dev_loop_workflow import MERGED_PR, TASK_QUEUE, _fake_activities

    monkeypatch.setattr(dev_loop, "MERGE_WATCH_HISTORY_FLOOR", 1)
    open_pr = PRState(
        found=True,
        pr_url=MERGED_PR.pr_url,
        repo=MERGED_PR.repo,
        number=MERGED_PR.number,
        state="OPEN",
        head_sha="deadbeef",
    )
    activities, _calls, investigate_ran, _ops = _fake_activities(released=True, pr_states=[open_pr, open_pr, MERGED_PR])
    wid = f"dev-loop-pre-m1-hop-{uuid.uuid4()}"
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[DevLoopWorkflow],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url="https://github.com/mctlhq/mctl-telegram/issues/964", work_item_id="wi-hop-1"),
            id=wid,
            task_queue=TASK_QUEUE,
        )
        with anyio.fail_after(30):
            await investigate_ran.wait()
        await handle.signal(DevLoopWorkflow.approve, {"approver": "alice", "approval_ref": "aar_test"})
        with anyio.fail_after(30):
            result = await handle.result()
        assert result.pr is not None and result.pr.state == "MERGED"
        run_id = handle.first_execution_run_id or handle.run_id
        n = 0
        while run_id:
            history = (await env.client.get_workflow_handle(wid, run_id=run_id).fetch_history()).to_json_dict()
            n += 1
            _write(f"dev_loop_hop_run{n}", history, wid)
            last = history["events"][-1]
            run_id = last.get("workflowExecutionContinuedAsNewEventAttributes", {}).get("newExecutionRunId")
        assert n >= 2, "the floor forced no hop"


async def test_dev_loop_post_gates_resume(env, monkeypatch):
    """A resume delivered onto a merge watch (past the gates): pre-M1 it is
    accepted and its execution `Succeeded` at once. The history M1 changes
    the behaviour for, so the one that matters most for replay."""
    from orchestrator.temporal import dispatcher as dx
    from orchestrator.temporal.activities.argo import WorkflowResult
    from orchestrator.temporal.constants import TASK_QUEUE
    from orchestrator.temporal.issue_ref import workflow_id_for
    from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef, MergeWatchResume
    from orchestrator.work_context.client import WorkItemClient
    from tests.test_execution_request_dispatch import DispatchFakeApi, _dispatcher, _wait_for
    from tests.test_execution_request_resume import _ledger_entry, _row, _submit_log, _worker
    from tests.test_work_context_resume_acceptance import URL, WID

    api = DispatchFakeApi()
    monkeypatch.setattr(
        WorkItemClient, "_request", lambda self, method, path, payload=None: api.request(method, path, payload)
    )
    monkeypatch.delenv(dx.ENABLED_ENV_VAR, raising=False)
    submit, _ = _submit_log()
    loop = workflow_id_for(URL)
    _ledger_entry(api, "temporal", loop, "Succeeded")
    watching = MergeWatchResume(
        service="mctl-telegram",
        slug="issue-431-dispatch-acceptance",
        deadline="2099-01-01T00:00:00Z",
        investigate=WorkflowResult(workflow_name="mctl-agents-investigate-fake", phase="Succeeded"),
        implement=WorkflowResult(workflow_name="mctl-agents-implement-fake", phase="Succeeded"),
        work_item_id=WID,
    )
    async with _worker(env, submit):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run,
            IssueRef(issue_url=URL, work_item_id=WID, resume=watching),
            id=loop,
            task_queue=TASK_QUEUE,
        )
        await handle.query(DevLoopWorkflow.work_context)
        rid = api.create_request("resume")
        outcome = await _dispatcher(env).dispatch_once()
        assert outcome.action == dx.FULFILLED and outcome.engine_ref == f"{loop}#{rid}"
        await _wait_for(lambda: _row(api, f"{loop}#{rid}")["phase"] == "Succeeded")
        await handle.terminate("recorded")
        _write("dev_loop_post_gates_resume", (await handle.fetch_history()).to_json_dict(), loop)


async def test_dev_loop_resumed(env, monkeypatch):
    """A resume delivered BEFORE the gates (approval flipped), run to its end."""
    from orchestrator.temporal.issue_ref import workflow_id_for
    from orchestrator.work_context.client import WorkItemClient
    from tests import test_execution_request_replay as rr
    from tests.test_execution_request_dispatch import DispatchFakeApi
    from tests.test_work_context_resume_acceptance import URL

    api = DispatchFakeApi()
    monkeypatch.setattr(
        WorkItemClient, "_request", lambda self, method, path, payload=None: api.request(method, path, payload)
    )
    rr._CURRENT_API[:] = [api]
    try:
        _write("dev_loop_resumed", await rr.record_resumed(env), workflow_id_for(URL))
    finally:
        rr._CURRENT_API.clear()


# -- the other seven workflow types ------------------------------------------


async def test_reconcile(env):
    from tests.replay_scenarios import record, scenario_by_name

    handle = await record(env.client, scenario_by_name("reconcile_apply"))
    _write("reconcile", (await handle.fetch_history()).to_json_dict(), handle.id)


async def test_incident_loop(env):
    from tests.replay_scenarios import record, scenario_by_name

    handle = await record(env.client, scenario_by_name("incident_loop"))
    _write("incident_loop", (await handle.fetch_history()).to_json_dict(), handle.id)


async def test_issue_poll(env):
    from orchestrator.temporal.activities.issue_poll import DirectiveScanResult, IssuePollActivityResult
    from orchestrator.temporal.workflows.issue_poll import IssuePollWorkflow, IssuePollWorkflowInput
    from tests.temporal_harness import Worker
    from tests.test_issue_poll_workflow import TASK_QUEUE, _fake_activities

    activities, received = _fake_activities(
        poll_result=IssuePollActivityResult(started=1, failures=0),
        directive_result=DirectiveScanResult(dispatched=2, replied=2, deferred=1, failed=0),
    )
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[IssuePollWorkflow], activities=activities):
        handle = await env.client.start_workflow(
            IssuePollWorkflow.run,
            IssuePollWorkflowInput(label="agents:intake", max_issues=5, max_directives=7),
            id=f"issue-poll-pre-m1-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        await handle.result()
    assert received["directive_max_directives"] == 7
    _write("issue_poll", (await handle.fetch_history()).to_json_dict(), handle.id)


async def test_implement_sweep_and_swept_child(env):
    from orchestrator.temporal.workflows.implement_sweep import ImplementSweepWorkflow, SweptImplementWorkflow
    from tests.temporal_harness import Worker
    from tests.test_implement_sweep_workflow import _DEFAULT_CHILD_ID, TASK_QUEUE, _fake_activities

    activities, received = _fake_activities()
    async with Worker(
        env.client,
        task_queue=TASK_QUEUE,
        workflows=[ImplementSweepWorkflow, SweptImplementWorkflow],
        activities=activities,
    ):
        handle = await env.client.start_workflow(
            ImplementSweepWorkflow.run,
            None,
            id=f"implement-sweep-pre-m1-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
            retry_policy=RetryPolicy(maximum_attempts=1),
        )
        result = await handle.result()
        child = env.client.get_workflow_handle(_DEFAULT_CHILD_ID)
        await child.result()
    assert result.submitted == 1 and len(received["submits"]) == 1
    _write("implement_sweep", (await handle.fetch_history()).to_json_dict(), handle.id)
    _write("swept_implement", (await child.fetch_history()).to_json_dict(), child.id)


async def test_scheduled_dispatch(env):
    from orchestrator.temporal.workflows.scheduled_dispatch import ScheduledDispatchWorkflow
    from tests.temporal_harness import Worker
    from tests.test_scheduled_dispatch_workflow import INPUT, OK, TASK_QUEUE, _acts

    seen, acts = _acts()
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[ScheduledDispatchWorkflow], activities=acts):
        handle = await env.client.start_workflow(
            ScheduledDispatchWorkflow.run, INPUT, id=f"scheduled-dispatch-pre-m1-{uuid.uuid4()}", task_queue=TASK_QUEUE
        )
        assert await handle.result() == OK
    assert seen["report"] == []
    _write("scheduled_dispatch", (await handle.fetch_history()).to_json_dict(), handle.id)


async def test_scheduled_dispatch_failure(env):
    """The failure branch too: dispatch fails non-retryably, the report runs."""
    from temporalio.client import WorkflowFailureError

    from orchestrator.temporal.workflows.scheduled_dispatch import ScheduledDispatchWorkflow
    from tests.temporal_harness import Worker
    from tests.test_scheduled_dispatch_workflow import INPUT, TASK_QUEUE, _acts

    seen, acts = _acts(dispatch_error=("RunNotObserved", True))
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[ScheduledDispatchWorkflow], activities=acts):
        handle = await env.client.start_workflow(
            ScheduledDispatchWorkflow.run,
            INPUT,
            id=f"scheduled-dispatch-fail-pre-m1-{uuid.uuid4()}",
            task_queue=TASK_QUEUE,
        )
        with pytest.raises(WorkflowFailureError):
            await handle.result()
    assert len(seen["report"]) == 1
    _write("scheduled_dispatch_failure", (await handle.fetch_history()).to_json_dict(), handle.id)


async def test_action_approval_wait(env, monkeypatch):
    from tests import test_action_approval_wait as t

    monkeypatch.setenv(t.pc.APPROVALS_ENV, t.pc.APPROVALS_MCTL_API)
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.setenv("MCTL_API_BASE_URL", "https://api.example.test")
    monkeypatch.delenv(t.aa.TTL_ENV, raising=False)
    fake = t.FakeMctlApi(t._Clock())  # type: ignore[arg-type]
    monkeypatch.setattr(t.aa, "_no_redirect_opener", lambda: fake)
    t.WORLD.clear()
    t.WORLD.update({"head": "sha-1", "effects": 0, "calls": [], "reads": 0, "api": fake})
    _write("action_approval_wait", await t.record(env, fake), None)
