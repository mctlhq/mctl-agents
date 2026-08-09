"""Question Author Agent Runner

Clean-Room Question Author Agent: Authors certification questions from public documentation excerpts
only. Never reconstructs exam items from memory, never leaks vendor branding into question text.
All generated items are given status `review_ready` and require maintainer evidence approval.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

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


def author_question(source_excerpt: str, objective: str) -> dict[str, Any]:
    logger.info("Authoring clean-room question for objective=%s", objective)
    return {
        "status": "review_ready",
        "objective": objective,
        "evidence_excerpt": source_excerpt,
    }
