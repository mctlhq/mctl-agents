"""Temporal activity for processing documentation deltas and invoking question authoring.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from temporalio import activity

from orchestrator.run_question_author import author_question

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocsDeltaActivityResult:
    source_id: str
    delta_classification: str
    status: str
    questions_generated: int = 0
    generated_question_ids: tuple[str, ...] = ()


@activity.defn
async def process_docs_delta_activity(
    source_id: str,
    delta_classification: str,
    url: str,
    target_repo: str = "mctlhq/mctl-academy",
    excerpt: str = "Documentation source excerpt supporting objective claim.",
    objective: str = "domain-1/foundations",
) -> DocsDeltaActivityResult:
    logger.info(
        "Processing docs delta for source_id=%s classification=%s url=%s target_repo=%s",
        source_id,
        delta_classification,
        url,
        target_repo,
    )
    if delta_classification in ("deprecated", "formatting_only"):
        # Deprecation or minor formatting does not trigger new question generation
        return DocsDeltaActivityResult(
            source_id=source_id,
            delta_classification=delta_classification,
            status="processed",
            questions_generated=0,
            generated_question_ids=(),
        )

    # Invoke clean-room question authoring
    q_data = author_question(source_excerpt=excerpt, objective=objective, source_id=source_id)
    qid = str(q_data["id"])

    return DocsDeltaActivityResult(
        source_id=source_id,
        delta_classification=delta_classification,
        status="processed",
        questions_generated=1,
        generated_question_ids=(qid,),
    )
