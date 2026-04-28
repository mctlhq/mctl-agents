---
name: scan-sibling-commits
description: Use when collecting user-visible changes from neighbouring mctl repos over a given period.
---

# Scan sibling commits

When the mctl-docs researcher hunts for doc gaps:

1. **Base path.** `BASE="${SIBLING_REPOS_PATH:-/Users/dmitriimashkov/PycharmProjects/mctlhq}"`.
   The repo list is in `CLAUDE.md`. Do not scan yourself (`mctl-docs`).

2. **Time range.** Default `--since="7 days ago"`. If you need a
   different range, factor in the date of the last mentor digest (look
   at `cd mctl-gitops && ls platform-gitops/agents-state/_mentor/digest/`
   and pick the freshest).

3. **Commands:**
```bash
# List user-visible commits
git -C "$BASE/<repo>" log --since="7 days ago" --pretty='%h|%ad|%s' --date=short --no-merges

# When unclear — list touched files
git -C "$BASE/<repo>" show --stat <sha>

# When still unclear — read a specific (short!) diff
git -C "$BASE/<repo>" show <sha> -- path/to/relevant.go | head -200
```

4. **Conventional-commits filter.** Keep only the prefixes:
   - `feat:` / `feat(scope):` — new user-facing capability
   - `fix:` / `fix(scope):` — fix for a user-visible bug
   - `docs:` / `docs(scope):` — only if it describes a new concept (not a typo fix)
   - any mention of `BREAKING CHANGE:` — always keep (migration note needed)

   Drop: `chore:`, `refactor:`, `test:`, `ci:`, `style:`, `build:`,
   `perf:` (unless perf changes user-observable behaviour).

5. **Cross-reference with docs.** Read `context/docs-tree.md` — it
   snapshots the structure of `docs.mctl.ai` with short descriptions.
   For each user-visible commit, decide
   "already documented / gap / stale" against that tree.

6. **mctl MCP check (optional).** If `mcp__mctl__*` is available, fetch
   the current production version of each repo. A commit ahead of
   production gets tagged "in-flight" — too early to document, may be reverted.

## Do not
- Do not clone anything. If a repo is missing, record it in the inbox
  and skip — the gap is infra, not docs.
- Do not interpret priority — that is the analyst's job.
- Do not read more than 200 lines of diff at a time. Stat + commit
  message + first hunk is usually enough to grasp the meaning.
