# mctl-agents

Multi-agent система для платформы mctl. Каждый сервис имеет своего агента-владельца, который:
- читает источники (changelog'и, GitHub releases, CVE, метрики из mctl MCP)
- складывает находки в `inbox/`
- оформляет топ-предложения в `proposals/<slug>/{requirements,design,tasks}.md`

Mentor-агент агрегирует proposals и выдаёт еженедельный дайджест.

## Структура

```
agents/
├── mctl-web/                  # один агент = один сервис
│   ├── CLAUDE.md              # роль и границы агента
│   ├── .claude/
│   │   ├── skills/            # переиспользуемые навыки
│   │   └── agents/            # sub-agents (researcher, analyst, spec-writer)
│   ├── context/               # архитектура, ADR, текущая версия
│   ├── inbox/                 # сырые находки researcher'а
│   └── proposals/             # оформленные spec-driven предложения
├── _mentor/                   # ментор платформы
│   ├── CLAUDE.md
│   └── digest/                # еженедельные дайджесты
config/
└── settings.py                # SERVICES, mctl MCP URL
orchestrator/
├── auth.py                    # OAuth ИЛИ API-ключ
├── run_service_agent.py       # запуск агента сервиса
├── run_mentor.py              # запуск ментора
└── run_all.py                 # параллельный прогон всех + ментор
```

## Auth: два режима

Код одинаково работает и с OAuth-токеном (твоя Claude Pro/Max подписка),
и с API-ключом (Console-биллинг). Выбор автоматический:

- если задан `CLAUDE_CODE_OAUTH_TOKEN` — используется он (личное использование, прототип)
- иначе используется `ANTHROPIC_API_KEY` (production)

Получить OAuth-токен:
```bash
npm install -g @anthropic-ai/claude-code
claude setup-token   # откроет браузер, выдаст sk-ant-oat01-...
```

## Запуск

```bash
pip install -r requirements.txt
cp .env.example .env
# отредактируй .env: положи либо CLAUDE_CODE_OAUTH_TOKEN, либо ANTHROPIC_API_KEY,
# плюс MCTL_TOKEN для доступа к https://api.mctl.ai/mcp

# один агент
python -m orchestrator.run_service_agent mctl-web

# ментор
python -m orchestrator.run_mentor

# всё целиком
python -m orchestrator.run_all
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
        |
        v
   proposals/<slug>/{requirements,design,tasks}.md  (status: proposed)
        |  (mentor or human flips status: accepted)
        v
   run_implementer.py  (Tier 2)  -->  feat/agents-<slug> + open PR
        |                              status: implemented, pr: <url>
        v
   run_shepherd.py  (Tier 3)  -->  drives the PR to merge
                                   status: merged | rejected | review-stuck
```

### Tier 2 — implementer

`orchestrator/run_implementer.py` turns an `accepted` proposal into a
real PR:

1. Clones the target sibling repo (`mctlhq/<svc>`) to a tmp worktree.
2. Creates branch `feat/agents-<slug>`.
3. Runs the per-service implementer sub-agent
   (`agents/<svc>/.claude/agents/implementer.md`) with `PROPOSAL_DIR`
   pointing at the gitops worktree. The agent reads the spec and
   commits the minimum viable change; it never pushes.
4. The Python wrapper pushes the branch and opens a PR via `gh pr create`.
5. Flips `.status.yaml` to `status: implemented` with the PR URL.

The same module also implements `--review-feedback <path>`, used by
the [Tier 3 shepherd](#tier-3--pr-shepherd) to address codex review
findings on an existing PR (no new branch, no new PR; pushes a
follow-up commit on the same head ref).

### Tier 3 — PR shepherd

`orchestrator/run_shepherd.py` drives implementer-opened PRs through
codex review iterations and merges them once review is clean and CI
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
```

The local one-shot needs the same env as the implementer: a
`GITHUB_TOKEN` with `repo` write scope on `mctlhq/*` (used by `gh
api` and `gh pr merge`) and either `CLAUDE_CODE_OAUTH_TOKEN`
(Pro/Max) or `ANTHROPIC_API_KEY` for the SDK call that summarises
codex findings. See `.env.example` for the full list.

**State machine.** Per tick, for every proposal whose status is in
`{implemented, review-fixing}` and that has a `pr:` URL:

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

- `chatgpt-codex-connector[bot]` — the **only** gating signal.
  `has_responded` is anchored to `pr.head_sha` (review with matching
  `commit_id`, line-anchored comment with matching `commit_id`,
  top-level "Didn't find any major issues" comment newer than
  `head_pushed_at`, or `+1` reaction on a `@codex review` trigger
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
