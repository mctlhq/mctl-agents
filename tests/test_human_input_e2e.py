"""Investigator -> human -> automatic continuation, inside one real
DevLoopWorkflow execution (mctlhq/mctl-agents#473).

The fake investigate CWFT of `_fake_activities` cannot run code, so the real
producer is driven from the fake `find_human_input_request`: it seals a
model-style draft with the loop identity the workflow actually passed in the
investigate params, exactly as the investigator would before publishing."""
from __future__ import annotations

import asyncio
import json
import uuid

import anyio
import pytest

from orchestrator import human_input as hi
from orchestrator import run_issue_investigator as rii
from orchestrator.context_snapshot import ExecutionCorrelation
from orchestrator.temporal.workflows.dev_loop import DevLoopWorkflow, IssueRef
from tests.temporal_harness import Worker
from tests.test_dev_loop_workflow import (
    TASK_QUEUE,
    _fake_activities,
    _response_payload,
    _wait_for_pending_request,
    env,  # noqa: F401 — pytest fixture
)

pytestmark = pytest.mark.anyio

ISSUE_URL = "https://github.com/mctlhq/mctl-telegram/issues/1"


class _ProducedRequests(list):
    """Answers `find_human_input_request` call by call from the real producer:
    the first read follows investigate call 1 (a draft is sealed), later reads
    follow a continuation that asks nothing."""

    def __init__(self, tmp_path, params_log):
        super().__init__([None, None])
        self._tmp_path = tmp_path
        self._params_log = params_log
        self.sealed = None

    def __getitem__(self, index):
        if index != 0:
            return None
        params = self._params_log[0]
        proposal = self._tmp_path / "proposal"
        draft = proposal / "human-input" / "draft.json"
        draft.parent.mkdir(parents=True, exist_ok=True)
        draft.write_text(json.dumps({
            "question": "Use library A or library B?",
            "reason": "the issue names two and prefers neither",
            "response": {"type": "single_choice", "options": ["library A", "library B"]},
        }))
        correlation = ExecutionCorrelation(
            agent="issue-investigator", environment="shadow",
            temporal_workflow_id=params["temporal_workflow_id"],
            temporal_run_id=params["temporal_run_id"],
            target_repository_sha="a" * 40, definition_version="1.0.0",
            definition_content_hash="sha256:" + "1" * 64, profile_version="1.2.0",
            profile_content_hash="sha256:" + "2" * 64, release_revision=1,
        )
        self.sealed, rejection = rii._seal_draft(
            proposal, correlation=correlation, work_item_id="mctl-telegram-1", issue_author="alice",
            prior_answers=[], issue_url=ISSUE_URL,
        )
        assert rejection == "" and self.sealed is not None
        return (proposal / "human-input" / "request.json").read_text()


async def test_investigator_asks_human_answers_loop_continues_then_approval_gate(env, tmp_path):  # noqa: F811
    wf_id = f"dev-loop-test-{uuid.uuid4()}"
    params_log: list[dict] = []
    produced = _ProducedRequests(tmp_path, params_log)
    activities, calls, investigate_ran, _ = _fake_activities(
        released=True, human_input_requests=produced, investigate_params_log=params_log,
    )
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run, IssueRef(issue_url=ISSUE_URL), id=wf_id, task_queue=TASK_QUEUE,
        )
        with anyio.fail_after(10):
            await investigate_ran.wait()
        # (a)+(b): the request the real producer sealed parks the workflow.
        with anyio.fail_after(10):
            while produced.sealed is None:  # noqa: ASYNC110 — polling a fake's state
                await asyncio.sleep(0.05)
        await _wait_for_pending_request(handle, produced.sealed.request_id)
        assert "mctl-agents-implement" not in calls

        # (c): a valid signal is the only operator action.
        payload = _response_payload(produced.sealed, value="library B")
        await handle.signal(DevLoopWorkflow.human_input_response, payload)
        with anyio.fail_after(15):
            while calls.count("mctl-agents-investigate") < 2:  # noqa: ASYNC110 — polling a fake's call list
                await asyncio.sleep(0.05)

        # (d): the continuation carries the answer, bound to the sealed request.
        answers = rii._parse_human_input_responses(params_log[1]["human_input_responses"])
        assert [(a["request_id"], a["request_hash"]) for a in answers] == [
            (produced.sealed.request_id, produced.sealed.request_hash)
        ]
        # (e): and the continuation prompt renders it.
        assert "library B" in rii._human_input_answers_block(answers)

        # (f): clarification is not approval — the implementer has not run.
        with anyio.fail_after(10):
            while True:
                state = await handle.query(DevLoopWorkflow.human_input_state)
                if state.state == "RUNNING":
                    break
                await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)
        assert "mctl-agents-implement" not in calls

        await handle.signal(DevLoopWorkflow.approve)
        result = await handle.result()

    assert result.human_input is not None and result.human_input.outcome == "answered"
    assert result.human_input.request_id == produced.sealed.request_id
    assert result.implement is not None and result.implement.phase == "Succeeded"
    assert hi.MAX_CLARIFICATION_ROUNDS >= 2
