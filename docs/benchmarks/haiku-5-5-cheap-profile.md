# Haiku 5.5 as the `cheap` profile default

Issue: mctlhq/mctl-agents#663. Measured 2026-10-17 between 08:25 and 08:28 UTC
with `claude-agent-sdk==0.2.168`, whose bundled CLI reports `2.1.301 (Claude Code)`.
`claude-haiku-5-5` needs CLI 2.1.296 or later, so no further SDK bump was needed.

## What was measured

`tools/model_smoke.py --eval` runs two fixed mechanical workloads, ten inputs
each, one tool-less single-turn query per input, JSON-only answers:

- **commit-type**: classify a one-line change description into one
  conventional-commit type (`feat`, `fix`, `docs`, `test`, `ci`, `refactor`,
  `chore`).
- **url-extract**: list every http(s) URL in a sentence, in order, without
  trailing punctuation; three inputs contain none (an e-mail address, an
  `ftp://` link and a bare `example.com`).

The inputs and expected answers are constants in the script, so both models
saw identical prompts. Each query runs with `tools=[]`, `setting_sources=[]`,
`strict_mcp_config=True` and claude.ai connectors and auto-memory disabled,
which keeps the operator's own Claude Code setup out of the prompt. A run fails
if the result reports any model other than the requested one, so the CLI's
background small-fast calls cannot leak into the numbers. Thinking was left at
the CLI's per-model default, the same as in production.

## Results

| workload | model | correct | malformed JSON | errors | median latency ms | input tokens | output tokens | cost USD |
|---|---|---|---|---|---|---|---|---|
| commit-type | claude-haiku-5-5 | 10/10 | 0/10 | 0/10 | 1344 | 9447 | 273 | 0.0020 |
| url-extract | claude-haiku-5-5 | 10/10 | 0/10 | 0/10 | 1254 | 9507 | 267 | 0.0020 |
| commit-type | claude-haiku-4-5 | 10/10 | 0/10 | 0/10 | 2592 | 7797 | 2565 | 0.0206 |
| url-extract | claude-haiku-4-5 | 10/10 | 0/10 | 0/10 | 1958 | 7863 | 2133 | 0.0185 |

Input tokens include cache creation and cache reads. Latency is the CLI's
`duration_ms` per query. Both models were fully correct with no malformed JSON.
Haiku 4.5 spent roughly ten times the output tokens, almost all of it default
thinking, which accounts for its higher cost and latency.

## Reproduce

```
uv run python tools/model_smoke.py --eval
CLAUDE_CHEAP_MODEL=claude-haiku-4-5 uv run python tools/model_smoke.py --eval
```

Both need real Claude credentials. Ten inputs per workload is a sanity check,
not a statistically strong comparison.

## Scope

This is evidence for **mechanical** tasks only. It says nothing about
judgement tasks (review verdicts, root-cause analysis, mentor digests), which
is why every committed task stays on `balanced`. Routing any task to `cheap`
needs its own evaluation.

## Remaining `claude-haiku-4-5` references

After this change `grep -rn claude-haiku-4-5` (excluding `.venv`) shows only:

| reference | kind | action |
|---|---|---|
| `CHANGELOG.md` (two entries) | release history | kept: historical |
| `docs/benchmarks/capability-discovery.md` | earlier benchmark that describes the CLI side calls of its time | kept: historical |
| `tests/fixtures/model-usage-records.json` | recorded usage of a dated model id | kept: historical data |
| `tests/test_usage_ledger.py` | asserts on that fixture | kept: historical data |
| `tools/model_smoke.py` docstring | rollback smoke and eval commands | rollback docs |
| `tests/test_model_policy.py` | rollback test (T3) | rollback docs |
