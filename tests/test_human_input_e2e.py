"""Investigator -> human -> automatic continuation, inside one real
DevLoopWorkflow execution on the Temporal test environment
(mctlhq/mctl-agents#473, task 7 / T8).

Nothing on the producer side is simulated. The fake investigate CWFT runs the
REAL `investigate()` (real `_run_agent` with a fake SDK client, real
`_seal_draft`, real publish) with the `temporal_workflow_id`,
`temporal_run_id` and `human_input_responses` the workflow actually put in
the CWFT params. The fake `find_human_input_request` only reads back what
that run published at `human-input/request.json`, as the real activity reads
it from gitops main. The "model" is a script: per call it writes the triplet
and, when the scenario says so, a clarification draft."""
from __future__ import annotations

import asyncio
import functools
import json
import uuid
from pathlib import Path

import anyio
import anyio.to_thread
import pytest
from temporalio.api.enums.v1 import EventType

from orchestrator import human_input as hi
from orchestrator import run_issue_investigator as rii
from orchestrator.temporal.workflows.dev_loop import APPROVAL_POLL_INTERVAL, DevLoopWorkflow, IssueRef
from tests import human_input_harness as harness_mod
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
ASK_HEADER = "## Asking for clarification"


def _asks(question: str):
    def model(prompt: str, proposal_dir: Path) -> None:
        harness_mod.write_triplet(proposal_dir)
        draft = proposal_dir / "human-input" / "draft.json"
        draft.parent.mkdir()
        draft.write_text(json.dumps({
            "question": question,
            "reason": "the issue names two options and prefers neither",
            "response": {"type": "single_choice", "options": ["library A", "library B"]},
        }))
    return model


def _answers_only(prompt: str, proposal_dir: Path) -> None:
    harness_mod.write_triplet(proposal_dir)


class _Loop:
    """The investigate CWFT (`hook`) and the gitops read (`published`)."""

    def __init__(self, tmp_path: Path, monkeypatch, models) -> None:
        self.harness = harness_mod.install(tmp_path, monkeypatch, grant=True, issue_url=ISSUE_URL)
        self.state_dir = tmp_path / "state"
        self.state_dir.mkdir()
        self.models = models
        self.params: list[dict] = []
        self.results: list[rii.InvestigateResult] = []

    async def hook(self, params: dict) -> None:
        self.params.append(params)
        self.harness.model = self.models[len(self.params) - 1]
        # investigate() is synchronous and runs its own anyio loop, so it gets
        # a worker thread, exactly as it gets its own process in the CWFT.
        result = await anyio.to_thread.run_sync(functools.partial(
            rii.investigate, params["issue_url"], state_dir=self.state_dir,
            temporal_workflow_id=params.get("temporal_workflow_id"),
            temporal_run_id=params.get("temporal_run_id"),
            human_input_responses=params.get("human_input_responses"),
        ))
        assert result.error is None, result.error
        self.results.append(result)

    def published(self) -> str | None:
        if not self.results:
            return None
        path = self.results[-1].proposal_dir / "human-input" / "request.json"
        return path.read_text() if path.is_file() else None

    def request(self, call: int) -> hi.HumanInputRequest:
        path = self.results[call].proposal_dir / "human-input" / "request.json"
        return hi.HumanInputRequest.from_dict(json.loads(path.read_text()))

    async def wait_for_calls(self, n: int) -> None:
        with anyio.fail_after(30):
            while len(self.results) < n:  # noqa: ASYNC110 — polling the fake CWFT's progress
                await asyncio.sleep(0.05)


async def _wait_for_approval_park(handle, *, resume_count: int) -> None:
    """The approval park, read from the workflow itself, never inferred
    from a sleep. There is no dedicated approval query, so two workflow-side
    facts are combined: `human_input_state` says the clarification gate is
    closed (RUNNING) after exactly `resume_count` answers, and the history's
    newest command is the approval-watch timer (`APPROVAL_POLL_INTERVAL`, the
    `wait_condition(self._approved or self._abandoned, timeout=...)` of the
    approval park) started after every activity completed, none in flight."""
    with anyio.fail_after(30):
        while True:
            state = await handle.query(DevLoopWorkflow.human_input_state)
            history = await handle.fetch_history()
            events = list(history.events)
            types = [e.event_type for e in events]
            scheduled = types.count(EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED)
            finished = sum(types.count(t) for t in (
                EventType.EVENT_TYPE_ACTIVITY_TASK_COMPLETED,
                EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED,
            ))
            last_scheduled = max(
                (i for i, t in enumerate(types) if t == EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED),
                default=-1,
            )
            approval_timer = any(
                e.event_type == EventType.EVENT_TYPE_TIMER_STARTED and i > last_scheduled
                and e.timer_started_event_attributes.start_to_fire_timeout.ToTimedelta()
                == APPROVAL_POLL_INTERVAL
                for i, e in enumerate(events)
            )
            if (
                state.state == "RUNNING" and state.resume_count == resume_count
                and scheduled == finished and approval_timer
            ):
                return
            await asyncio.sleep(0.05)


async def test_investigator_asks_human_answers_loop_continues_then_approval_gate(
    env, tmp_path, monkeypatch,  # noqa: F811
):
    loop = _Loop(tmp_path, monkeypatch, [_asks("Use library A or library B?"), _answers_only])
    activities, calls, _investigate_ran, _ = _fake_activities(
        released=True, investigate_hook=loop.hook, human_input_reader=loop.published,
    )
    wf_id = f"dev-loop-test-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run, IssueRef(issue_url=ISSUE_URL), id=wf_id, task_queue=TASK_QUEUE,
        )
        # (a) The real producer sealed a request bound to THIS execution.
        await loop.wait_for_calls(1)
        sealed = loop.request(0)
        assert sealed.execution.temporal_workflow_id == wf_id == loop.params[0]["temporal_workflow_id"]
        assert sealed.execution.temporal_run_id == loop.params[0]["temporal_run_id"]
        assert sealed.round == 1
        assert sealed.requested_from.actor_refs == ("github:alice",)
        assert ASK_HEADER in loop.harness.prompts[0]

        # (b) ...and it parks the workflow.
        await _wait_for_pending_request(handle, sealed.request_id)
        assert "mctl-agents-implement" not in calls

        # (c) A valid signal is the only operator action.
        await handle.signal(DevLoopWorkflow.human_input_response, _response_payload(sealed, value="library B"))
        await loop.wait_for_calls(2)

        # (d) The continuation carries the answer, bound to the sealed request.
        answers = rii._parse_human_input_responses(loop.params[1]["human_input_responses"])
        assert [(a["request_id"], a["request_hash"]) for a in answers] == [
            (sealed.request_id, sealed.request_hash)
        ]
        # (e) The prompt the continuation sent the model renders it: built by
        # the real `_build_prompt` inside investigate(), not by a helper here.
        continuation_prompt = loop.harness.prompts[1]
        assert rii._human_input_answers_block(answers) in continuation_prompt
        assert "library B" in continuation_prompt and "RESOLVED" in continuation_prompt
        # The answered request is gone from what was published, and the
        # marker records it without the value.
        assert loop.published() is None
        marker = json.loads((loop.results[1].proposal_dir / "human-input" / "answered.json").read_text())
        assert [m["request_id"] for m in marker] == [sealed.request_id]
        assert "library B" not in json.dumps(marker)

        # (f) Clarification is not approval: parked at the approval gate,
        # implementer not run.
        await _wait_for_approval_park(handle, resume_count=1)
        assert "mctl-agents-implement" not in calls
        assert calls.count("mctl-agents-investigate") == 2

        # (g) approve, and only then the implementer runs.
        await handle.signal(DevLoopWorkflow.approve)
        result = await handle.result()

    assert calls.index("mctl-agents-implement") > calls.index("mctl-agents-approve")
    assert result.human_input is not None and result.human_input.outcome == "answered"
    assert result.human_input.request_id == sealed.request_id
    assert result.implement is not None and result.implement.phase == "Succeeded"


async def test_round_two_parks_again_and_the_third_answer_hits_the_round_bound(
    env, tmp_path, monkeypatch,  # noqa: F811
):
    """T8, second case. A model that asks on EVERY run: each continuation
    seals the next round and the workflow parks again. With
    MAX_CLARIFICATION_ROUNDS = 3 the third answer is the last one the bound
    admits: the continuation after it carries all three answers, is no
    longer invited to ask, and the draft it writes anyway is refused at the
    round limit, so nothing is published and the loop falls through to the
    approval park with resume_count == MAX_CLARIFICATION_ROUNDS."""
    rounds = hi.MAX_CLARIFICATION_ROUNDS
    questions = [f"Question for round {n}?" for n in range(1, rounds + 2)]
    loop = _Loop(tmp_path, monkeypatch, [_asks(q) for q in questions])
    activities, calls, _investigate_ran, _ = _fake_activities(
        released=True, investigate_hook=loop.hook, human_input_reader=loop.published,
    )
    wf_id = f"dev-loop-test-{uuid.uuid4()}"
    async with Worker(env.client, task_queue=TASK_QUEUE, workflows=[DevLoopWorkflow], activities=activities):
        handle = await env.client.start_workflow(
            DevLoopWorkflow.run, IssueRef(issue_url=ISSUE_URL), id=wf_id, task_queue=TASK_QUEUE,
        )
        sealed_ids = []
        for n in range(1, rounds + 1):
            await loop.wait_for_calls(n)
            request = loop.request(n - 1)
            # Round n was sealed by the run that saw n-1 answers, and parks.
            assert request.round == n
            assert request.question == questions[n - 1]
            assert ASK_HEADER in loop.harness.prompts[n - 1]
            await _wait_for_pending_request(handle, request.request_id)
            state = await handle.query(DevLoopWorkflow.human_input_state)
            assert (state.round, state.resume_count) == (n, n - 1)
            assert "mctl-agents-implement" not in calls
            sealed_ids.append(request.request_id)
            await handle.signal(DevLoopWorkflow.human_input_response, _response_payload(request, value="library A"))

        # The continuation after the third answer.
        await loop.wait_for_calls(rounds + 1)
        final_answers = rii._parse_human_input_responses(loop.params[rounds]["human_input_responses"])
        assert [a["request_id"] for a in final_answers] == sealed_ids
        assert ASK_HEADER not in loop.harness.prompts[rounds]
        assert loop.results[rounds].outcome_reason == rii.HUMAN_INPUT_REJECTED_REASON
        assert loop.published() is None
        marker = json.loads((loop.results[rounds].proposal_dir / "human-input" / "answered.json").read_text())
        assert [m["request_id"] for m in marker] == sealed_ids

        await _wait_for_approval_park(handle, resume_count=rounds)
        assert "mctl-agents-implement" not in calls
        assert calls.count("mctl-agents-investigate") == rounds + 1

        await handle.signal(DevLoopWorkflow.approve)
        result = await handle.result()

    assert result.human_input is not None and result.human_input.outcome == "answered"
    assert result.human_input.request_id == sealed_ids[-1]
    assert result.implement is not None and result.implement.phase == "Succeeded"
