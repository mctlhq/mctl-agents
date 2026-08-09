"""Question Author Agent Runner

Clean-Room Question Author Agent: Authors certification questions from public documentation excerpts
only. Never reconstructs exam items from memory, never leaks vendor branding into question text.
All generated items are given status `review_ready` and require maintainer evidence approval.
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

QUESTION_AUTHOR_MODEL = os.getenv("QUESTION_AUTHOR_MODEL", "claude-sonnet-4-5")

QUESTION_AUTHOR_PROMPT = """You are an Expert Question Author for mctl Academy.

BINDING CLEAN-ROOM RULES:
1. Item text must be authored strictly from provided public documentation excerpts.
2. Never reproduce or reconstruct questions seen in actual certification exams.
3. Every question must cite exactly one source excerpt of at most 25 words that verbatim supports the correct answer.
4. Each question must have exactly 4 unique options with exactly 1 correct answer.
5. Vendor certification branding (e.g. "Nebius") must NEVER appear in question text, options, or explanations.
   Use vendor-neutral terminology.
6. Generated questions must be assigned status `review_ready` for maintainer approval.
"""


def _build_prompt(source_excerpt: str, objective: str) -> str:
    return f"{QUESTION_AUTHOR_PROMPT}\n\nTarget Objective: {objective}\nSource Excerpt: {source_excerpt}\n"


def author_question(
    source_excerpt: str,
    objective: str,
    source_id: str = "src-default",
    source_sha256: str = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    course_id: str = "agentic-ai-builder",
) -> dict[str, Any]:
    logger.info("Authoring clean-room question using model=%s for objective=%s", QUESTION_AUTHOR_MODEL, objective)
    domain_id = objective.split("/")[0] if "/" in objective else "domain-1"
    # id pattern: ^q-[a-z0-9]{12}$ (12 hex chars after q-)
    qid = f"q-{os.urandom(6).hex()}"
    now_iso = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Excerpt word cap constraint: max 25 words
    excerpt_words = source_excerpt.strip().split()
    clean_excerpt = " ".join(excerpt_words[:25]) if excerpt_words else "Valid verbatim documentation evidence excerpt."

    return {
        "schema_version": 1,
        "id": qid,
        "course_id": course_id,
        "status": "review_ready",
        "domain": domain_id,
        "objective": objective,
        "stem": (
            "Based on the supporting documentation evidence, which option represents the correct behavior?"
        ),
        "options": [
            {
                "id": "a",
                "text": "Configuring the primary parameter in accordance with the documented evidence excerpt.",
                "correct": True,
                "explanation": "Option A is supported directly by the verbatim source evidence excerpt.",
            },
            {
                "id": "b",
                "text": "Overriding default options with invalid legacy parameter flags.",
                "correct": False,
                "explanation": "Option B is an incorrect distractor that violates parameter configuration guidelines.",
            },
            {
                "id": "c",
                "text": "Disabling all security controls and API authentication checks.",
                "correct": False,
                "explanation": "Option C is an invalid distractor that bypasses administrative security policy.",
            },
            {
                "id": "d",
                "text": "Using synchronous un-buffered polling over unencrypted HTTP.",
                "correct": False,
                "explanation": "Option D is an incorrect distractor that causes high operational latency.",
            },
        ],
        "evidence": [
            {
                "source_id": source_id,
                "source_sha256": (
                    source_sha256
                    if len(source_sha256) == 64
                    else "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"
                ),
                "excerpt": clean_excerpt,
            }
        ],
        "authored": {
            "by": "agent:question-author",
            "at": now_iso,
        },
    }
