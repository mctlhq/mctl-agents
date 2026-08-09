"""Tests for DocsDeltaWorkflow and process_docs_delta_activity.
"""
from __future__ import annotations

import re

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
    source_id = "src-function-calling"
    valid_snapshot_sha = "1ced0e48d880014ddcc5b1f91266fb0fe230fef380e414fb267ea8d5c20adc2b"

    prompt = _build_prompt(excerpt, objective)
    assert "BINDING CLEAN-ROOM RULES" in prompt
    assert "Target Objective: domain-2/function-calling" in prompt

    res = author_question(excerpt, objective, source_id=source_id, source_sha256=valid_snapshot_sha)
    assert res["status"] == "review_ready"
    assert res["objective"] == objective
    assert res["course_id"] == "agentic-ai-builder"
    assert re.match(r"^q-[a-z0-9]{12}$", res["id"])
    assert "stem" in res
    assert len(res["options"]) == 4
    assert res["options"][0]["correct"] is True
    assert res["evidence"][0]["source_id"] == source_id
    assert res["evidence"][0]["source_sha256"] == valid_snapshot_sha
    assert res["evidence"][0]["excerpt"] == excerpt
    assert res["authored"]["by"] == "agent:question-author"


def test_question_author_rejects_missing_or_invalid_sha256() -> None:
    excerpt = "Valid documentation evidence excerpt."
    objective = "domain-1/foundations"
    source_id = "src-test"

    with pytest.raises(ValueError, match="source_sha256"):
        author_question(excerpt, objective, source_id=source_id, source_sha256="")

    with pytest.raises(ValueError, match="source_sha256"):
        author_question(excerpt, objective, source_id=source_id, source_sha256="invalid-short-hash")


def test_docs_delta_workflow_input() -> None:
    inp = DocsDeltaWorkflowInput(source_id="src-test", delta_classification="deprecated")
    assert inp.source_id == "src-test"
    assert inp.delta_classification == "deprecated"
    assert inp.target_repo == "mctlhq/mctl-academy"


def test_candidate_verification_against_academy_schema_rules() -> None:
    """Integration contract test asserting that author_question candidates strictly

    comply with mctl-academy's content schema and evidence verification rules.
    """
    excerpt = "Function calling allows LLMs to return JSON structured data matching a tool schema."
    objective = "domain-2/function-calling"
    source_id = "src-function-calling"
    valid_snapshot_sha = "1ced0e48d880014ddcc5b1f91266fb0fe230fef380e414fb267ea8d5c20adc2b"

    candidate = author_question(
        source_excerpt=excerpt,
        objective=objective,
        source_id=source_id,
        source_sha256=valid_snapshot_sha,
        course_id="agentic-ai-builder",
    )

    # 1. Structural schema compliance
    assert candidate["schema_version"] == 1
    assert re.match(r"^q-[a-z0-9]{12}$", candidate["id"])
    assert candidate["status"] == "review_ready"
    assert candidate["course_id"] in ("agentic-ai-builder", "ai-cloudops-engineer", "ai-leader")
    assert candidate["domain"] == "domain-2"
    assert candidate["objective"] == objective
    assert isinstance(candidate["stem"], str) and len(candidate["stem"]) > 0

    # 2. Options compliance: 4 options, exactly 1 correct
    options = candidate["options"]
    assert len(options) == 4
    assert [o["id"] for o in options] == ["a", "b", "c", "d"]
    assert sum(1 for o in options if o["correct"] is True) == 1
    for opt in options:
        assert isinstance(opt["text"], str) and len(opt["text"]) > 0
        assert isinstance(opt["explanation"], str) and len(opt["explanation"]) > 0

    # 3. Evidence provenance and snapshot sha256 compliance
    evidence = candidate["evidence"]
    assert len(evidence) == 1
    ev = evidence[0]
    assert ev["source_id"] == source_id
    assert ev["source_sha256"] == valid_snapshot_sha
    assert len(ev["source_sha256"]) == 64
    assert len(ev["excerpt"].split()) <= 25

    # 4. Authored provenance block
    assert candidate["authored"]["by"] == "agent:question-author"
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", candidate["authored"]["at"])


