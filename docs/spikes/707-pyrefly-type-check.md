# Spike #707: Pyrefly as a fast type-check gate for DevLoop inner loops

STATUS: DRAFT. The harness, config, mutation corpus and workflow are
complete; every timed number below must come from one run of
`.github/workflows/spike-707-bench.yml` on a GitHub-hosted `ubuntu-latest`
runner (operator decision 2026-10-23). Cells marked `RUNNER` are filled from
that run's `results.json`; the recommendation is not final until then.
Remove the `STATUS: DRAFT` line when done; `tests/test_spike_707_report.py`
then enforces the final shape. Findings labelled "local" below are
non-timing observations from the implementer clone and are reproducible by
the harness.

## Environment

| Item | Value |
| --- | --- |
| Baseline mctl-agents SHA | `68f10afcb8a96d7b1c54d5d56819d8e39979c275` (git worktree, 136 checked files) |
| Python | 3.12 (setup-uv) |
| uv | 0.11.11 |
| mypy | 2.3.1 (from `uv.lock`) |
| Pyrefly | 1.4.0 via `uv tool run pyrefly==1.4.0` (never in `uv.lock`) |
| CPU / cores / memory / OS | RUNNER (printed by the workflow's "Print environment" step) |
| Workflow run | RUNNER (link) |

## Configuration (A and B)

- (A) auto-migrated: no Pyrefly config; Pyrefly reports
  `using settings imported from [tool.mypy] in your pyproject.toml
  (preset: legacy)`. Its own `dump-config` output is stored verbatim in
  `docs/spikes/707/diagnostics/pyrefly-A-config-dump.txt` (RUNNER artifact).
- (B) explicit: `docs/spikes/707/pyrefly.toml`, passed with `--config`:
  includes `orchestrator`, `config`, `tools`; Python 3.12; search path
  includes the repo root and `tools/` (mypy resolves `tools/*.py` as
  top-level modules; without this Pyrefly emits `missing-import` for
  `check_binding_hash` / `publish_agent_release`); the venv
  site-packages; a `tests/**` sub-config with no errors, mirroring the
  `tests.*` `ignore_errors` override. Nothing stricter.
- (C) stricter mode: not run (optional extra).

## Checked file sets

Local observation: mypy (`-v`), Pyrefly (A) and Pyrefly (B) all cover the same
136 files; both set differences are empty (`results.json` `file_sets`). The
runner run must reproduce this before any result below is compared.

## Latency

All RUNNER. Cold: 10 fresh-process runs per checker (mypy cache removed);
warm mypy (incremental cache kept) is reported separately and labelled.
Edit loop: 20 apply/check/revert iterations per checker over mutations
01-07. Pyrefly has no persistent cache in CLI mode.

| Metric | mypy | Pyrefly |
| --- | --- | --- |
| Cold median / p95 | RUNNER | RUNNER |
| Warm-cache median / p95 (mypy only) | RUNNER | n/a |
| Edit-loop median / p95 (20 iterations) | RUNNER | RUNNER |

Local sanity (not for the decision): one cold mypy run took about 32 s and one
Pyrefly run about 2.8 s on the implementer clone.

## Lint-job timing

RUNNER: one lint-job-equivalent (`uv sync --locked` with an empty uv cache,
Ruff, checker) with phases timed separately in `results.json` `lint_job`.
Checker-only speedups must not be presented as CI speedups: the existing
`lint` job comment states setup dominates.

## Diagnostic comparison

Local: mypy reports 0 diagnostics on the baseline (exit 0). Pyrefly reports 15
(A) and 13 (B). Classification (to be confirmed against the runner artifact):

| Finding | Config | Class |
| --- | --- | --- |
| `bad-argument-type` `Any \| None` -> `str` `kind`/`name` (capability.py:818, context_snapshot.py:979, execution_evidence.py:1234, execution_identity.py:464, resolver.py:774) | A, B | credible additional defect candidates (Optional flowing into constructors; mypy sees `Any`) |
| `bad-argument-type` `str \| Any` -> `int` `schema_version` (commerce.py:332, 333) | A, B | credible additional defect candidate |
| `bad-argument-type` `strategy` (context_assembly.py:1713) | A, B | credible additional defect candidate |
| `missing-import` for `check_binding_hash`, `publish_agent_release` (tools/) | A only | migration/config mismatch (fixed by the B search path) |
| `unsupported-operation` `dict[str, str]` item set (tools/diagram_facts.py:117, 122, 123) | A, B | credible additional defect candidate or inference difference; needs manual triage on the runner artifact |
| `unbound-name` (run_issue_investigator.py:2505, 2507) | B only | credible additional defect candidate; absent under the `legacy` preset of A |

mypy findings missed by Pyrefly: none (mypy has zero findings). Each "credible"
row must be hand-verified before the final report; no source is fixed in this
spike.

## Mutation detection

Local detection run (full table in `docs/spikes/707/mutations/README.md`):
mypy and Pyrefly (A and B) both catch patches 01-07 with an actionable
file:line:cause message; both miss patch 08 (wrong type through an
unannotated parameter), which is the expected soft-mode `Any` boundary.

## Suppression compatibility

Local: Pyrefly honours all 10 existing `# type: ignore[...]` comments (no
diagnostic at those lines in A or B). Stripping the comments in a scratch
copy shows which ones guard a defect Pyrefly independently sees:

| Site | Code | mypy | Pyrefly | Pyrefly sees the defect without it |
| --- | --- | --- | --- | --- |
| capability_gateway.py:617 | arg-type | honoured | honoured | yes (`bad-return`) |
| run_implementer.py:5479 | arg-type | honoured | honoured | no |
| run_issue_investigator.py:4111 | arg-type | honoured | honoured | no |
| run_issue_investigator.py:4433 | arg-type | honoured | honoured | no |
| run_shepherd.py:4638 | arg-type | honoured | honoured | yes (`bad-argument-type`) |
| runtime_assurance.py:612 | operator | honoured | honoured | yes (`unsupported-operation`) |
| runtime_assurance.py:643 | arg-type | honoured | honoured | yes (`bad-argument-type`) |
| runtime_assurance.py:1627 | union-attr | honoured | honoured | yes (`missing-attribute`) |
| runtime_assurance.py:1628 | union-attr | honoured | honoured | yes (`missing-attribute`) |
| temporal/worker.py:221 | arg-type | honoured | honoured | no |

Pyrefly does not mirror `warn_unused_ignores`: the four rows marked "no" are
ignores Pyrefly considers unnecessary yet does not flag. mypy remains the only
checker that enforces that setting.

## Library notes

To be confirmed on the runner with the stored diagnostics: no `temporalio`,
`mcp`, `httpx` or `claude_agent_sdk` import errors appear in the local Pyrefly
(B) output; `types-pyyaml` is resolved from the synced venv through the
site-packages path. Any stub or inference issue found in the artifact is
recorded here.

## Inner-loop assessment

Default integration point: the "Service-specific notes" of
`agents/mctl-agents/.claude/agents/implementer.md`, which today only runs
`python -c "import ..."` and targeted pytest. Candidate command, pinned by
version and committed config:

    uvx pyrefly==1.4.0 check --config docs/spikes/707/pyrefly.toml

The production image is built with `uv sync --locked --no-dev`, so it has no
checker. Delivery options: per-run `uvx` download (cost: `lint_job`
`pyrefly_cold_fetch_s`, `pyrefly_download_cache_delta_mb`, RUNNER) or a pinned
image layer. Loop timings (checker -> Ruff -> targeted pytest, both
sequences, five seeded patches) are in `results.json` `loop`.

## Metrics table

| Metric | mypy | Pyrefly |
| --- | --- | --- |
| Exit code / diagnostics on baseline | 0 / 0 | 1 / 15 (A), 1 / 13 (B) |
| Files checked | 136 | 136 |
| Mutations caught (of 8) | 7 | 7 |
| Cold median / p95 | RUNNER | RUNNER |
| Edit-loop median / p95 | RUNNER | RUNNER |
| Loop static feedback (checker + Ruff) | RUNNER | RUNNER |

## Recommendation

Recommendation: PENDING (candidate: PYREFLY_INNER_LOOP_ONLY; to be decided
from the runner numbers against the issue's decision criteria).

If PYREFLY_INNER_LOOP_ONLY is confirmed: a Pyrefly pass never satisfies the
mypy gate; mypy and the `lint` job stay unchanged.

## Follow-up issue body

To be written with the final recommendation (omitted for KEEP_MYPY).
