"""Run the mentor — aggregates proposals/ from every service agent into a weekly digest.

Usage:
    python -m orchestrator.run_mentor
"""
import re
from datetime import date
from pathlib import Path

import anyio
from claude_agent_sdk import query

from config.settings import MENTOR_DIR, MENTOR_MODEL, SERVICES
from orchestrator.auth import ensure_auth_for_sdk
from orchestrator.options import build_mentor_options


# Digest filename: YYYY-WNN.md (ISO 8601 year + week).
_DIGEST_RE = re.compile(r"^(\d{4})-W(\d{2})\.md$")


def _subtract_iso_weeks(year: int, week: int, weeks: int) -> tuple[int, int]:
    """Return (year, week) that is `weeks` ISO-weeks before the given (year, week).

    Implemented via date arithmetic so it handles year boundaries and 53-week years.
    """
    anchor = date.fromisocalendar(year, week, 1)
    shifted = date.fromordinal(anchor.toordinal() - weeks * 7)
    iso_year, iso_week, _ = shifted.isocalendar()
    return iso_year, iso_week


def _rotate_old_digests(digest_dir: Path, keep_weeks: int = 8) -> None:
    """Move digest files older than the last `keep_weeks` ISO-weeks to archive/.

    Called before the SDK writes the new digest. Files matching `YYYY-WNN.md`
    older than (latest_week - keep_weeks) are renamed into `digest_dir/archive/`.
    Non-matching files (README.md, archive/, etc.) are left untouched.
    The function is idempotent — silent when there is nothing to rotate.
    """
    if not digest_dir.is_dir():
        return

    candidates: list[tuple[tuple[int, int], Path]] = []
    for entry in digest_dir.iterdir():
        if not entry.is_file():
            continue
        match = _DIGEST_RE.match(entry.name)
        if not match:
            continue
        iso_year = int(match.group(1))
        iso_week = int(match.group(2))
        # Validate that (year, week) is a real ISO 8601 date.
        # The regex permits W00-W99, but only W01-W52 (or W53 in long years)
        # are valid; date.fromisocalendar raises ValueError otherwise.
        try:
            date.fromisocalendar(iso_year, iso_week, 1)
        except ValueError:
            print(f"[mentor] skipping invalid digest filename: {entry.name}")
            continue
        candidates.append(((iso_year, iso_week), entry))

    if not candidates:
        return

    latest_year, latest_week = max(key for key, _ in candidates)
    threshold_year, threshold_week = _subtract_iso_weeks(
        latest_year, latest_week, keep_weeks
    )
    threshold = (threshold_year, threshold_week)

    archive_dir = digest_dir / "archive"
    moved = 0
    for key, path in candidates:
        # Strictly older than threshold — keep `keep_weeks` most recent inclusive.
        if key < threshold:
            archive_dir.mkdir(parents=True, exist_ok=True)
            path.rename(archive_dir / path.name)
            moved += 1

    if moved:
        print(
            f"[mentor] rotated {moved} digest(s) older than "
            f"{threshold_year}-W{threshold_week:02d} → archive/"
        )


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
    # Retention is enforced by the orchestrator (deterministic, no token spend);
    # the agent itself is not asked to manage old files.
    digest_dir = Path(MENTOR_DIR) / "digest"
    _rotate_old_digests(digest_dir)

    options = build_mentor_options(MENTOR_DIR, MENTOR_MODEL)
    print(f"\n=== Running mentor ({MENTOR_MODEL}) ===\n")

    async for message in query(prompt=build_prompt(), options=options):
        print(message)


def main() -> None:
    ensure_auth_for_sdk()
    anyio.run(run_mentor)


if __name__ == "__main__":
    main()
