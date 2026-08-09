"""Tests for DocsDeltaWorkflow and process_docs_delta_activity.
"""
from __future__ import annotations

import pytest

from orchestrator.run_question_author import _build_prompt, author_question
from orchestrator.temporal.activities.docs_delta import DocsDeltaActivityResult, process_docs_delta_activity
from orchestrator.temporal.workflows.docs_delta import DocsDeltaWorkflowInput


@pytest.mark.anyio
async def test_process_docs_delta_activity() -> None:
    res = await process_docs_delta_activity(
        source_id="src-nebius-func",
        delta_classification="capability_added",
        url="https://docs.nebius.com/function-calling.md",
        target_repo="mctlhq/mctl-academy",
    )
    assert isinstance(res, DocsDeltaActivityResult)
    assert res.source_id == "src-nebius-func"
    assert res.delta_classification == "capability_added"
    assert res.status == "processed"
    assert res.questions_generated == 1
    assert len(res.generated_question_ids) == 1

    dep_res = await process_docs_delta_activity(
        source_id="src-nebius-legacy",
        delta_classification="deprecated",
        url="https://docs.nebius.com/legacy.md",
        target_repo="mctlhq/mctl-academy",
    )
    assert dep_res.questions_generated == 0


def test_question_author_agent() -> None:
    excerpt = "Function calling allows LLMs to return JSON structured data matching a tool schema."
    objective = "domain-2/function-calling"

    prompt = _build_prompt(excerpt, objective)
    assert "BINDING CLEAN-ROOM RULES" in prompt
    assert "Target Objective: domain-2/function-calling" in prompt

    res = author_question(excerpt, objective)
    assert res["status"] == "review_ready"
    assert res["objective"] == objective
    assert res["evidence"][0]["excerpt"] == excerpt[:25]
    assert res["certification"] == "agentic-ai-builder"


def test_docs_delta_workflow_input() -> None:
    inp = DocsDeltaWorkflowInput(source_id="src-test", delta_classification="deprecated")
    assert inp.source_id == "src-test"
    assert inp.delta_classification == "deprecated"
    assert inp.target_repo == "mctlhq/mctl-academy"
