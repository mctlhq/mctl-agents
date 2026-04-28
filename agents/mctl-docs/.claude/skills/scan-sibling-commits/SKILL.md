---
name: scan-sibling-commits
description: Use when collecting user-visible changes from neighbouring mctl repos over a given period.
---

# Scan sibling commits

When the mctl-docs researcher hunts for doc gaps you have two modes —
**Mode A: local clone** (preferred, fast) and **Mode B: GitHub API
fallback** (for cluster runs where only `mctl-gitops` is cloned).

## 1. Base path and repo list
`BASE="${SIBLING_REPOS_PATH:-/Users/dmitriimashkov/PycharmProjects/mctlhq}"`.
The repo list is in `CLAUDE.md`. Do not scan yourself (`mctl-docs`).

## 2. Time range
Default `--since="7 days ago"` (ISO8601 for the API: compute as `now - 7d`).
If you need a different range, factor in the date of the last mentor digest
(look at `cd mctl-gitops && ls platform-gitops/agents-state/_mentor/digest/`
and pick the freshest).

## 3. Per-repo: pick a mode

For each `<repo>` in the list:

### Mode A — local clone (when `$BASE/<repo>/.git` exists)
Probe with `Bash`: `test -d "$BASE/<repo>/.git" && echo present || echo missing`.

If `present`:
```bash
# List user-visible commits
git -C "$BASE/<repo>" log --since="7 days ago" --pretty='%h|%ad|%s' --date=short --no-merges

# When unclear — list touched files
git -C "$BASE/<repo>" show --stat <sha>

# When still unclear — read a specific (short!) diff
git -C "$BASE/<repo>" show <sha> -- path/to/relevant.go | head -200
```

### Mode B — GitHub API fallback (when the local clone is missing)
If `missing` — do NOT record "no signal", use `WebFetch` instead:

URL: `https://api.github.com/repos/mctlhq/<repo>/commits?since=<ISO8601>&per_page=100`

Where `<ISO8601>` is e.g. `2026-04-21T00:00:00Z` (for `--since="7 days ago"`
relative to the current date).

`WebFetch` parameters:
- `url`: the URL above
- `prompt`: something like:
  > Return a JSON-like list of objects, one per commit. For each commit extract:
  > - `sha` (the short 7-char hex from the `sha` field)
  > - `date` (the `commit.author.date` field, ISO8601 truncated to YYYY-MM-DD)
  > - `message` (the FIRST LINE of `commit.message` only — drop everything after the first `\n`)
  > - `url` (the `html_url` field)
  > Skip merge commits (commits with more than one parent in the `parents` array).
  > Output as plain text, one commit per line, format: `sha|date|message|url`.
  > Also: at the very end, on a separate line, report whether the response `Link` header
  > contains `rel="next"` (e.g. `LINK_NEXT: yes` or `LINK_NEXT: no`). If you cannot see
  > the response headers, instead emit `LINK_NEXT: unknown`.

WebFetch returns text in this format — parse it line by line (the last line
is the `LINK_NEXT:` marker).

**Headers**: WebFetch does not let you set arbitrary headers directly. If
the prompt cannot pull the data because the public API is unauthenticated
and gets rate-limited, fall back to `Bash curl` instead. The curl variant
is also simpler for pagination (you can read the `Link` header via `-D-`
or `curl -i`):
```bash
curl -sD /tmp/hdr.txt \
     -H "Authorization: Bearer $GITHUB_TOKEN" \
     -H "Accept: application/vnd.github+json" \
     "https://api.github.com/repos/mctlhq/<repo>/commits?since=<ISO8601>&per_page=100&page=1" \
   | jq -r '.[] | select((.parents | length) <= 1) | "\(.sha[0:7])|\(.commit.author.date[0:10])|\(.commit.message | split("\n")[0])|\(.html_url)"'
# Check for the next page:
grep -i '^link:' /tmp/hdr.txt | grep -q 'rel="next"' && echo HAS_NEXT || echo NO_NEXT
```

The curl variant is more reliable — it sets `Authorization`, lifts the
rate-limit from 60/h to 5000/h and returns lines in the format we need.

#### Pagination
GitHub `/commits` returns at most 100 commits per page — for a 7-day window
on active repos that is plenty, but **always paginate**, otherwise older
commits get silently truncated.

Algorithm:
1. Start with `page=1`, `per_page=100`.
2. After each request, inspect the `Link` header (curl: `-D-` / `-i`;
   WebFetch: ask for the `LINK_NEXT` marker via the prompt above). If
   `rel="next"` is present, increment `page` and repeat.
3. Alternative (when `Link` is not visible — e.g. WebFetch returned
   `LINK_NEXT: unknown`): increment `page` until the response returns an
   **empty array** (`[]`).
4. **Sanity cap: 5 pages per repo maximum** (≈500 commits over the window
   — an order of magnitude more than an active repo produces in 7 days).
   If you hit the cap, stop and **flag it explicitly** in the output, e.g.
   `note: <repo> hit 5-page cap, digest may be incomplete`, so the analyst
   knows the data is truncated.

With `per_page=100` and the typical <100 commits/week, one request is
enough and `Link: rel="next"` is absent — that is the normal happy path.

### Touched files / diff in Mode B
- File list: `GET /repos/mctlhq/<repo>/commits/<sha>` (the `files[].filename`
  field plus `additions`/`deletions`).
- Diff: `Accept: application/vnd.github.v3.diff` on the same endpoint
  returns a raw diff. Truncate with `head -200`.

## 4. Conventional-commits filter
Keep only the prefixes:
- `feat:` / `feat(scope):` — new user-facing capability
- `fix:` / `fix(scope):` — fix for a user-visible bug
- `docs:` / `docs(scope):` — only if it describes a new concept (not a typo fix)
- any mention of `BREAKING CHANGE:` — always keep (migration note needed)

Drop: `chore:`, `refactor:`, `test:`, `ci:`, `style:`, `build:`, `perf:`
(unless perf changes user-observable behaviour).

## 5. Cross-reference with docs
Read `context/docs-tree.md` — it snapshots the structure of `docs.mctl.ai`
with short descriptions. For each user-visible commit, decide
"already documented / gap / stale" against that tree.

## 6. mctl MCP check (optional)
If `mcp__mctl__*` is available, fetch the current production version of
each repo. A commit ahead of production gets tagged "in-flight" — too
early to document, may be reverted.

## Rate limits
- Authenticated GitHub API (with `GITHUB_TOKEN`): **5000 req/h**.
- 7 sibling repos × 1 list-call/run = 7/h baseline. Even with extra
  touched-files calls per interesting commit, the order of magnitude is
  50–100 req/run. Plenty of headroom.
- Without a token (`GITHUB_TOKEN` empty): 60 req/h public — you will
  almost certainly hit the limit. In that case write
  `no signal: <repo> rate-limited` in the inbox and skip.

## Do not
- Do not clone anything. In Mode A, if the repo is missing, fall through
  to Mode B. In Mode B, if `$GITHUB_TOKEN` is empty and the public API
  rate-limits, write "no signal" and skip.
- Do not interpret priority — that is the analyst's job.
- Do not read more than 200 lines of diff at a time. Stat + commit
  message + first hunk is usually enough to grasp the meaning.
