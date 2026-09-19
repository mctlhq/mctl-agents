# mctl-agents

Multi-agent system for the mctl platform. Each service has its own owner
agent, which:
- reads sources (changelogs, GitHub releases, CVEs, metrics from the mctl MCP)
- drops findings into `inbox/`
- writes up top proposals in `proposals/<slug>/{requirements,design,tasks}.md`

A mentor agent aggregates proposals and produces a weekly digest.

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
│   └── digest/                # weekly digests
config/
└── settings.py                # SERVICES, mctl MCP URL
orchestrator/
├── auth.py                    # OAuth OR API key
├── run_service_agent.py       # run a service agent
├── run_mentor.py              # run the mentor
└── run_all.py                 # run everything in parallel + mentor
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

# mentor
uv run python -m orchestrator.run_mentor

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
        |  (approve: DevLoop-сигнал / mctl-agents-approve → status: accepted)
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

The proposal then follows the normal path: a human approves it (the
DevLoopWorkflow's `approve()` signal submits the `mctl-agents-approve`
CWFT, which commits the `proposed → accepted` flip to gitops; outside a
DevLoop the same operation can be run directly), Tier 2 implements it,
and the PR carries `Closes <repo>#<N>`
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

`accepted` is swept, not just processed on request: Temporal's
`implement-sweep-mctl-agents-schedule` (every 15 min, mctl-agents#412) reads
every proposal's committed status from gitops `main`, and submits
`mctl-agents-implement` for any `accepted` proposal that carries no `pr:`,
no unexpired `attempt` lease, no `blocked`/`approval-missing` marker, that
carries explicit execution authorization, whose `updated_at` is older than
the stranding grace period (`IMPLEMENT_SWEEP_GRACE_MINUTES`, default 20),
and whose derived DevLoopWorkflow id is not in the currently-running set.

Execution authorization is a separate, fail-closed question from the
write-time `control.requires_human_approval` check, and is deliberately not
layered on it: an absent `control` block correctly means "this record never
asked for approval" to a writer checking its own write, but it must never
mean "approval not required" to something submitting execution on a record
nobody is watching. Only a verified, non-anonymous `approval.approved_by`
authorizes a sweep submit today (`proposal_state.execution_authorization`);
a separately-defined explicit autonomy policy is the reserved second path
and no policy is defined yet. Provenance of who WROTE a record is not
authorization — a writer allowlist was tried and rejected by the 2026-09-19
product decision on mctl-agents#412, and `ProposalStateRef` no longer
carries `updated_by` at all so one cannot be rebuilt. Unauthorized
proposals are quarantined from execution with their specs untouched and
reported on `ImplementSweepResult.unauthorized` for human triage, under two
distinct reasons: `legacy auto-accepted / unreviewed` (never approved — the
69 `incident-*` records that have sat in `accepted` since August) and
`approved with no recorded approver identity` (a real approval whose path
recorded no approver, fixed by re-approving rather than by triage). That last check is
what makes the sweep safe beside a live DevLoop: `mctl_trigger_approve` and
the incident responder's direct `status: accepted` write both flip a
proposal to `accepted` with no owner, and before this the sweep that used to
promote it (an Argo cron) had been suspended since the Temporal migration —
so those proposals sat untouched until a human ran `mctl_trigger_implementer`
by hand. Up to `IMPLEMENT_SWEEP_MAX_SUBMITS` (default 5) proposals are
submitted per tick, scoped to `{service, slug}` on the same admission queue
DevLoopWorkflow's own implement step uses. Every candidate that survives the
scan is logged as `STRANDED service=... slug=... reason=...`, whether it is
submitted, over the per-tick cap, already being swept, or over its pre-start
retry budget; proposals the scan itself filtered out are reported in
`StrandedScanResult.skipped` instead, and the quarantined ones additionally
on `.unauthorized`. An unknown active-DevLoop set, a failed stranding scan,
or an unknown pre-start retry budget each skip the whole tick with a
`skipped_reason` rather than risk double-running an implementer or
submitting past a bound.

An `accepted` proposal whose `control.requires_human_approval` is set but
carries no verified `approval.approved_by` is neither retried nor treated
as a plain skip: `mctl-agents-approve` only performs the `proposed ->
accepted` flip, so it is a no-op on a proposal that is already `accepted`,
and this combination can never run by any supported path
(mctl-agents#349). The implementer classifies it `blocked` (code
`approval-missing`), writes a durable `blocked: {code, since, message,
remedy}` block to `.status.yaml` once (idempotent — unchanged runs leave
the file byte-identical), and a batch whose only proposals are blocked
exits `45` instead of `0` so the condition surfaces on the workflow. The
supported recovery is to re-publish the proposal in `proposed` status,
where the approve flip actually records an approver.

The same module also implements `--review-feedback <path>`, used by
the [Tier 3 shepherd](#tier-3--pr-shepherd) to address code review
findings on an existing PR (no new branch, no new PR; pushes a
follow-up commit on the same head ref).

Both the initial push and every follow-up push carry an `ExecutionClaim`
(ADR-010 phase 2, `docs/adr/010-lifecycle-ownership-contract.md`): a
short-lived mutual-exclusion lease checked immediately before `git push`,
with `--force-with-lease=<branch>:<sha>` as the authoritative fence
underneath it. Below `LIFECYCLE_ROLLOUT_MODE=enforce` the claim is advisory
only. Two lease durations are tunable via env:
`LIFECYCLE_CLAIM_LEASE_SECONDS_IMPLEMENT` (default `7800`, i.e. 130
minutes, the `attempt` lease it replaces) for the initial implement
attempt, and `LIFECYCLE_CLAIM_LEASE_SECONDS_REVIEW` for a
review-remediation follow-up. The review value is a *floor* of `1800`
(30 minutes, one `MERGE_POLL_INTERVAL`) under
`IMPLEMENTER_TIMEOUT_SECONDS + 2 x IMPLEMENTER_COMMAND_TIMEOUT_SECONDS`,
so the lease outlives the run it guards: with the stock `900 + 2 x 300`
the floor wins at 1800 seconds, and raising `IMPLEMENTER_TIMEOUT_SECONDS`
raises the lease with it. Both variables are lengthen-only: a value below
the computed lease is refused with a log line and the computed one is used,
so an override can widen a claim but never make it expire under its own run.
`.env.example` ships them commented out all the same.

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

# One-shot: fix review findings on a steward-owned repo without merging.
python -m orchestrator.run_shepherd \
    --service mctl-telegram --slug issue-481-idempotency-key-scope --fix-only
```

The local one-shot needs the same env as the implementer: a
`GITHUB_TOKEN` with `repo` write scope on `mctlhq/*` (used by `gh
api` and `gh pr merge`) and either `CLAUDE_CODE_OAUTH_TOKEN`
(Pro/Max) or `ANTHROPIC_API_KEY` for the SDK call that summarises
codex findings. See `.env.example` for the full list.

**Per-service ownership modes.** PR lifecycle ownership is split by repo
*and* by stage. `_service_mode(service)` resolves one of three modes:

- **full** (default) — discover, review, fix and merge.
- **fix-only** (`SHEPHERD_FIX_ONLY_SERVICES`, or every proposal in a run
  started with `--fix-only`) — discover, review and push follow-up
  commits, but `decide()` returns `defer-merge` instead of `merge`; merge
  is left to another PR lifecycle (e.g. `mctl-claude-remote`'s
  pr-steward). A service listed in both `SHEPHERD_FIX_ONLY_SERVICES` and
  `SHEPHERD_SKIP_SERVICES` resolves to fix-only (with a `warn:` line),
  so a gitops rollout that adds the new variable before removing the old
  one converges to the intended behaviour.
- **skip** (`SHEPHERD_SKIP_SERVICES`) — discover nothing; the service is
  owned end-to-end by another PR lifecycle.

`NEVER_MERGE_SERVICES` (currently `{"mctl-academy"}`) is a code
constant, not an env var: such a service never resolves to full, and
`merge_pr()` independently refuses to merge it — content publication
stays gated on a human CODEOWNER regardless of environment or
`--fix-only`.

**State machine.** Normal mode drives proposals in
`{implemented, review-fixing, in-progress}`. Missing PR URLs are recovered
from GitHub before the decision loop. Reconcile mode additionally repairs
`accepted`, `error`, `review-stuck`, `needs-triage`, and terminal drift without
reviewing, fixing, or merging a PR.

```python
def decide(pr, codex_review, *, fix_only=False):
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
    return ("defer-merge", None) if fix_only else ("merge", None)
```

Decisions:

- **wait** — codex still reviewing, draft PR, CI not green, or merge
  state blocked. Leave `.status.yaml` alone; next tick re-evaluates.
- **address-review** — codex left P1/P2 findings on the current head
  SHA. Build a JSON bundle of the findings via the shepherd
  sub-agent (`agents/_shepherd/shepherd.md`), persist it to a temp
  file, and fork `run_implementer.py --review-feedback <path>` so it
  pushes a follow-up commit on the existing branch. Unchanged by
  fix-only mode — this is the stage the shepherd keeps for
  steward-owned repos.
- **merge** — codex clean, CI green, merge state mergeable, and the
  service is not fix-only or in `NEVER_MERGE_SERVICES`. Calls
  `gh pr merge --merge --delete-branch --match-head-commit <SHA>` so
  a push that lands between review and merge cannot smuggle
  unreviewed code through. On HEAD-SHA mismatch we fall back to
  `wait` and the next tick re-evaluates.
- **defer-merge** — codex clean, CI green, merge state mergeable, but
  the service is in fix-only mode: merge is owned by another PR
  lifecycle. Records `merge_owner: pr-steward` in `.status.yaml`
  (via the change-only writer, so repeated ticks produce no new
  gitops commit) and leaves `status` untouched. `merge_pr()` is never
  called.
- **flip-to-merged** — human (or the steward) merged the PR out of
  band. Record `merge_commit` and flip the proposal to terminal
  `merged`; clears `merge_owner` if it was set.
- **flip-to-rejected** — human closed without merging. Flip to
  terminal `rejected` with the close comment in `notes:`; clears
  `merge_owner` if it was set.

**Review-attempt cap.** The outer loop tracks `review_attempts:` in
`.status.yaml`. After five consecutive `address-review` ticks
without resolving the findings, the next tick flips the proposal to
`status: review-stuck` (terminal) instead of forking the
implementer again. The pure `decide()` function does not see the
counter — it stays trivially testable with hand-built fixtures.

**Bot signals.**

- `claude[bot]` — the primary reviewer. It alone drives
  `has_responded`, anchored to `pr.head_sha` (review with matching
  `commit_id`, line-anchored comment with matching `commit_id`,
  top-level "No P1/P2 findings" comment newer than
  `head_pushed_at`, or `+1` reaction on a `@claude review` trigger
  newer than `head_pushed_at`). Any signal predating the head push
  is ignored. Its P1/P2 findings gate the merge.
- `chatgpt-codex-connector[bot]` (Codex Review) — second **gating**
  signal for findings only (#67): its P1/P2 on the current head
  route to `address-review` exactly like claude's, but its
  presence/absence never drives `has_responded` — the connector's
  trigger is best-effort and does not fire on every push, so a PR
  must never wait on it.
- `copilot-pull-request-reviewer[bot]` — observed only. Findings
  ride along to the per-tick operator log so they are visible
  without gating the merge.

**Tests.** `pytest tests/test_run_shepherd.py` covers every branch
of `decide()`, the head-SHA anchor on stale findings, the
`MAX_REVIEW_ATTEMPTS` outer-loop cap, and end-to-end happy + loop
paths driving `process_one()` against a real `tmp_path` worktree
fixture with the GitHub API + implementer subprocess mocked at the
module boundary.

**Adopted PRs (mctlhq/mctl-agents#334).** A pull request opened by hand — or
by any tool that is not `run_implementer.py` — has no proposal on disk, so
`_discover_refs` cannot see it even when a gating bot leaves a blocking
P1/P2 finding on it. `orchestrator/pr_adoption.py` adds a second, lightweight
durable record beside `proposals/`: a `PRRef` written to
`agents-state/<service>/adopted-prs/pr-<number>/.prref.yaml`, which the
existing `process_one` / `run_implementer --review-feedback` path then
drives exactly like a proposal's PR.

The whole feature is default-off and inert until an operator sets both:

- `SHEPHERD_ADOPT_PRS` (default `false`) — the master switch. `--adopt-prs`
  forces it on for a single local run.
- `SHEPHERD_ADOPT_REPOS` (default empty) — the allowlist of repos eligible
  for adoption. Empty means nothing is adoptable even with the switch on.
- `SHEPHERD_ADOPT_MAX_PRS_PER_TICK` (default `1`) — bound on adoption
  records processed per tick, so the loop stays bounded regardless of how
  many blocking-finding PRs a repo has open.

A candidate PR is adopted only when it is same-repository (never a fork),
its head branch is not the implementer's own `feat/agents-*` prefix, no
proposal already owns it, no live `DevLoopWorkflow` owns it, the service
does not resolve to `SKIP`, and — when the lifecycle ownership rollout is at
least `observe` — the ownership store admits this shepherd. An adoption
record is **always `FIX_ONLY`**: `decide()` can only ever return
`defer-merge` for it, `merge_pr()` is never reached, and adoption therefore
grants remediation, never merge authority (mctlhq/mctl-agents#344).

**Durability caveat.** The shepherd ClusterWorkflowTemplate in `mctl-gitops`
stages only `proposals/**` into its gitops commit today; `adopted-prs/**`
lands separately in `mctlhq/mctl-gitops#1278`. Until that PR merges, every
`.prref.yaml` this feature writes lives only in the pod's gitops worktree
and is discarded at the end of each tick — `review_attempts`,
`harness_failures` and `refusals` reset to zero every run. The shepherd
prints a startup warning naming both facts whenever adoption is enabled.
Do not enable `SHEPHERD_ADOPT_PRS` in production until both PRs are merged.
