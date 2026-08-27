# mctl-agents

Multi-agent system for the mctl platform. Each service has its own owner
agent, which:
- reads sources (changelogs, GitHub releases, CVEs, metrics from the mctl MCP)
- drops findings into `inbox/`
- writes up top proposals in `proposals/<slug>/{requirements,design,tasks}.md`

A mentor agent aggregates proposals and produces a weekly digest. A
platform-reporter agent reads live mctl MCP state (tenants, services,
incidents, resource usage) and writes a weekly operational health report.

## Structure

```
agents/
├── mctl-web/                  # one agent = one service
│   ├── CLAUDE.md              # agent role and boundaries
│   ├── .claude/
│   │   ├── skills/            # reusable skills
│   │   └── agents/            # sub-agents (researcher, analyst, spec-writer)
│   ├── context/               # architecture, ADRs, current version
│   ├── inbox/                 # raw researcher findings
│   └── proposals/             # finalized spec-driven proposals
├── _mentor/                   # platform mentor
│   ├── CLAUDE.md
│   └── digest/                # weekly proposal digests
├── _platform-reporter/        # weekly operational health
│   ├── CLAUDE.md
│   └── health/                # YYYY-WNN.md reports
config/
└── settings.py                # SERVICES, mctl MCP URL
orchestrator/
├── auth.py                    # OAuth OR API key
├── run_service_agent.py       # run a service agent
├── run_mentor.py              # run the mentor
├── run_platform_reporter.py   # weekly operational health from mctl MCP
└── run_all.py                 # run everything in parallel + mentor + reporter
```

## Auth: two modes

The code works the same way with either an OAuth token (your Claude Pro/Max
subscription) or an API key (Console billing). The choice is automatic:

- if `CLAUDE_CODE_OAUTH_TOKEN` is set, it's used (personal use, prototyping)
- otherwise `ANTHROPIC_API_KEY` is used (production)

Get an OAuth token:
```bash
npm install -g @anthropic-ai/claude-code
claude setup-token   # opens a browser, gives you sk-ant-oat01-...
```

## Running it

```bash
uv sync            # installs from uv.lock, including the dev group (pytest)
cp .env.example .env
# edit .env: set either CLAUDE_CODE_OAUTH_TOKEN or ANTHROPIC_API_KEY,
# plus MCTL_TOKEN for access to https://api.mctl.ai/mcp

# `uv sync` creates .venv but doesn't put it on PATH — `uv run` (or
# `source .venv/bin/activate`) is what actually uses the locked interpreter.

# one agent
uv run python -m orchestrator.run_service_agent mctl-web

# mentor (proposal digest)
uv run python -m orchestrator.run_mentor

# weekly operational health from mctl MCP
uv run python -m orchestrator.run_platform_reporter

# everything, all at once
uv run python -m orchestrator.run_all

# issue-driven: turn a GitHub issue into a proposal
uv run python -m orchestrator.run_issue_investigator \
    --issue-url https://github.com/mctlhq/mctl-telegram/issues/123
```

## Architecture

The proactive R&D pipeline runs in three tiers. Each tier is a
deterministic Python module under `orchestrator/` that delegates
language-y judgement to a Claude sub-agent prompt. Status is tracked
on disk in `platform-gitops/agents-state/<svc>/proposals/<slug>/.status.yaml`,
which the workflows commit back to gitops `main` so the whole pipeline
is observable from `git log`.

```
researcher / analyst / spec-writer  (Tier 1: per-service rotation)
        |                                  run_issue_investigator.py
        |                              (issue-driven entry, on demand)
        |                                          |
        v                                          v
   proposals/<slug>/{requirements,design,tasks}.md  (status: proposed)
        |  (mentor or human flips status: accepted)
        v
   run_implementer.py  (Tier 2)  -->  feat/agents-<slug> + open PR
        |                              status: implemented, pr: <url>
        v
   run_shepherd.py  (Tier 3)  -->  drives the PR to merge
                                   status: merged | rejected | review-stuck
```

### Issue-driven entry — investigator

The proactive Tier 1 rotation is not the only way a proposal is born.
`orchestrator/run_issue_investigator.py` takes a **GitHub issue** (a
human-filed feature request) and converts it into the same
`proposals/<slug>/` triplet:

1. Parses `--issue-url`, reads the issue via `gh issue view`.
2. Derives a deterministic slug `issue-<N>-<kebab-title>` and an
   idempotency guard: a proposal already past `proposed` is left alone.
3. Clones the target repo read-only so the agent can ground the design
   in real code, then runs the investigator sub-agent prompt.
4. Writes `.status.yaml` with `status: proposed` plus a `source` block
   linking back to the issue, and comments the proposal link on the issue.

The proposal then follows the normal path: a human flips it to
`accepted`, Tier 2 implements it, and the PR carries `Closes <repo>#<N>`
(read from the `source` block) so the issue auto-closes on merge.
Triggered on demand — by the `mctl_trigger_issue` MCP tool or an operator
submitting the `mctl-agents-investigate` workflow.

### Tier 2 — implementer

`orchestrator/run_implementer.py` turns an `accepted` proposal into a
real PR:

1. Queries GitHub for the deterministic `feat/agents-<slug>` branch and
   canonical PR. An existing open/merged/closed result is projected into
   `.status.yaml`; the model is not called.
2. If no prior result exists, clones the target sibling repo
   (`mctlhq/<svc>`) to a tmp worktree and creates the result branch.
3. Runs the per-service implementer sub-agent
   (`agents/<svc>/.claude/agents/implementer.md`) with `PROPOSAL_DIR`
   pointing at the gitops worktree. The agent reads the spec and
   commits the minimum viable change; it never pushes.
4. The Python wrapper pushes the branch and opens a PR via `gh pr create`.
5. Flips `.status.yaml` to `status: implemented` with the PR URL. Any
   incomplete/no-commit attempt moves to `needs-triage` and is never
   automatically retried.

The scheduled workflow processes at most one accepted proposal per run.
There is no unfiltered `--force` mode or automatic second-account retry.
An operator retries by reviewing the failure and moving that one proposal
from `needs-triage` back to `accepted`.

The same module also implements `--review-feedback <path>`, used by
the [Tier 3 shepherd](#tier-3--pr-shepherd) to address code review
findings on an existing PR (no new branch, no new PR; pushes a
follow-up commit on the same head ref).

### Tier 3 — PR shepherd

`orchestrator/run_shepherd.py` drives implementer-opened PRs through
code review iterations and merges them once review is clean and CI
is green. The shepherd is the second half of the proactive pipeline
— without it, `implemented` proposals sit forever waiting for a human
to merge.

**Cron cadence.** Once the companion ClusterWorkflowTemplate lands in
`mctl-gitops`, a `CronWorkflow` runs the shepherd every 30 minutes
with `concurrencyPolicy: Forbid`. Until then, run it on demand:

```bash
# All implemented / review-fixing proposals across every service.
python -m orchestrator.run_shepherd

# A specific PR (the typical local one-shot during development).
python -m orchestrator.run_shepherd --service mctl-web --slug wrangler-cve-0933

# Override the per-tick budget.
python -m orchestrator.run_shepherd --budget 2.00

# Discover-only — no SDK calls, no merges.
python -m orchestrator.run_shepherd --dry-run

# GitHub-first projection repair for every service; no SDK or merge.
python -m orchestrator.run_shepherd --reconcile
```

The local one-shot needs the same env as the implementer: a
`GITHUB_TOKEN` with `repo` write scope on `mctlhq/*` (used by `gh
api` and `gh pr merge`) and either `CLAUDE_CODE_OAUTH_TOKEN`
(Pro/Max) or `ANTHROPIC_API_KEY` for the SDK call that summarises
codex findings. See `.env.example` for the full list.

**State machine.** Normal mode drives proposals in
`{implemented, review-fixing, in-progress}`. Missing PR URLs are recovered
from GitHub before the decision loop. Reconcile mode additionally repairs
`accepted`, `error`, `review-stuck`, `needs-triage`, and terminal drift without
reviewing, fixing, or merging a PR.

```python
def decide(pr, codex_review):
    if pr.merged:
        return "flip-to-merged", pr.merge_commit
    if pr.closed_unmerged:
        return "flip-to-rejected", pr.close_comment_or_default
    if pr.is_draft or not codex_review.has_responded:
        return "wait", None
    findings = codex_review.findings_p1_p2(at=pr.head_sha)
    if findings:
        return "address-review", findings
    if pr.merge_state_status not in {"CLEAN", "HAS_HOOKS", "UNSTABLE"}:
        return "wait", None
    if not pr.checks_green:
        return "wait", None
    return "merge", None
```

Decisions:

- **wait** — codex still reviewing, draft PR, CI not green, or merge
  state blocked. Leave `.status.yaml` alone; next tick re-evaluates.
- **address-review** — codex left P1/P2 findings on the current head
  SHA. Build a JSON bundle of the findings via the shepherd
  sub-agent (`agents/_shepherd/shepherd.md`), persist it to a temp
  file, and fork `run_implementer.py --review-feedback <path>` so it
  pushes a follow-up commit on the existing branch.
- **merge** — codex clean, CI green, merge state mergeable. Calls
  `gh pr merge --merge --delete-branch --match-head-commit <SHA>` so
  a push that lands between review and merge cannot smuggle
  unreviewed code through. On HEAD-SHA mismatch we fall back to
  `wait` and the next tick re-evaluates.
- **flip-to-merged** — human merged the PR out of band. Record
  `merge_commit` and flip the proposal to terminal `merged`.
- **flip-to-rejected** — human closed without merging. Flip to
  terminal `rejected` with the close comment in `notes:`.

**Three-attempt cap.** The outer loop tracks `review_attempts:` in
`.status.yaml`. After three consecutive `address-review` ticks
without resolving the findings, the next tick flips the proposal to
`status: review-stuck` (terminal) instead of forking the
implementer again. The pure `decide()` function does not see the
counter — it stays trivially testable with hand-built fixtures.

**Bot signals.**

- `claude[bot]` — the **only** gating signal.
  `has_responded` is anchored to `pr.head_sha` (review with matching
  `commit_id`, line-anchored comment with matching `commit_id`,
  top-level "No P1/P2 findings" comment newer than
  `head_pushed_at`, or `+1` reaction on a `@claude review` trigger
  newer than `head_pushed_at`). Any signal predating the head push
  is ignored.
- `copilot-pull-request-reviewer[bot]` — observed only. Findings
  ride along to the per-tick operator log so they are visible
  without gating the merge.

**Tests.** `pytest tests/test_run_shepherd.py` covers every branch
of `decide()`, the head-SHA anchor on stale findings, the
3-attempt outer-loop cap, and end-to-end happy + loop paths driving
`process_one()` against a real `tmp_path` worktree fixture with the
GitHub API + implementer subprocess mocked at the module boundary.
