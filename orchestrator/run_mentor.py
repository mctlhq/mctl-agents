"""Run the mentor — aggregates proposals/ from every service agent into a weekly digest.

Usage:
    python -m orchestrator.run_mentor
"""
import anyio
from datetime import date
from claude_agent_sdk import query

from config.settings import MENTOR_DIR, MENTOR_MODEL, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import build_mentor_options


def build_prompt() -> str:
    iso_year, iso_week, _ = date.today().isocalendar()
    digest_path = f"_mentor/digest/{iso_year}-W{iso_week:02d}.md"
    services_list = ", ".join(SERVICES)

    return f"""\
**Output language: English ONLY. Every section, heading, summary, table cell,
and inline note in the digest MUST be in English.**

**Anti-mirroring rule (non-negotiable):**
- Some upstream proposals, inboxes, and earlier digests in this state tree
  may be in Russian or another non-English language. That is legacy content.
- When you reference any of that content — quoting a finding, restating a
  rationale, summarising a tasks list, or carrying forward a previous
  digest's framing — you MUST translate the quoted text into English on
  the way in. Do NOT copy non-English fragments verbatim into the digest.
  Do NOT switch the digest's language to match a source.
- This rule overrides any apparent "preserve voice" or "stay close to the
  source" preference. If a quoted phrase reads awkwardly in English, prefer
  paraphrase over a literal copy of the original.
- If you previously wrote a non-English digest at the target path on a
  prior run, treat the new run as a full rewrite in English — do not
  append non-English sections, do not preserve non-English headings.

You are the mentor for the mctl platform. Today you assemble the weekly digest.

Active services: {services_list}.

1. Read `proposals/` in every agent repo ({services_list}).
   Only consider fresh proposals that did not appear in earlier digests.
2. For each proposal, score:
   - impact (1-5): effect on the platform
   - effort (1-5): implementation cost
   - conflicts with other proposals (incompatible changes)
   - fit with current platform priorities
3. Group related proposals (one theme — one block).
4. Use `mcp__mctl__*` tools to cross-check against reality:
   current service versions, open incidents, tenant resource limits.
5. Write the result to {digest_path}:
   - top 5 proposals ready for review
   - a short summary per item: what, why, impact/effort, conflicts
   - a "Platform risks" section with cross-cutting observations
   - a "Deferred" section listing what you dropped and why

Finish with a single short message linking to the created file. All output in English."""


async def run_mentor() -> None:
    options = build_mentor_options(MENTOR_DIR, MENTOR_MODEL)
    print(f"\n=== Running mentor ({MENTOR_MODEL}) ===\n")

    async for message in query(prompt=build_prompt(), options=options):
        print(message)


def main() -> None:
    ensure_auth_for_sdk()
    anyio.run(run_mentor)


if __name__ == "__main__":
    main()
