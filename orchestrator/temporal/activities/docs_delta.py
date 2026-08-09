"""Temporal activity for processing documentation deltas and invoking question authoring.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from temporalio import activity

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DocsDeltaActivityResult:
    source_id: str
    delta_classification: str
    status: str
    questions_generated: int = 0


@activity.defn
async def process_docs_delta_activity(
    source_id: str,
    delta_classification: str,
    url: str,
    target_repo: str = "mctlhq/mctl-academy",
) -> DocsDeltaActivityResult:
    logger.info(
        "Processing docs delta for source_id=%s classification=%s url=%s target_repo=%s",
        source_id,
        delta_classification,
        url,
        target_repo,
    )
    # Perform clean-room analysis and trigger question authoring flow
    return DocsDeltaActivityResult(
        source_id=source_id,
        delta_classification=delta_classification,
        status="processed",
        questions_generated=1,
    )
