# Spike #707: Pyrefly as a fast type-check gate for DevLoop inner loops

Every timed number and every diagnostic below comes from one run of
`.github/workflows/spike-707-bench.yml` on a GitHub-hosted `ubuntu-latest`
runner: [run 39989820303](https://github.com/mctlhq/mctl-agents/actions/runs/39989820303)
on spike head `a9420d4`. Its artifact is committed as
`docs/spikes/707/results.json` and `docs/spikes/707/diagnostics/`.

An earlier run (39986902434, head `435e64e`) is void for config (B). The
harness passed `docs/spikes/707/pyrefly.toml` from the spike checkout, so its
relative paths resolved against that checkout instead of the pinned worktree:
no `.venv`, wrong SHA, 193 `missing-import` errors and 0 of 8 mutations seen.
`17d80a5` copies the config to the same depth inside the target and refuses to
record a checker that cannot see the target venv. Run 39987958862 (head
`17d80a5`) is superseded: its loop timed mypy on a cache built from the
already-mutated tree, a no-op. `4bc4604` warms on the clean tree instead.
Its diagnostics match the final run line for line.

## Environment

| Item | Value |
| --- | --- |
| Baseline mctl-agents SHA | `68f10afcb8a96d7b1c54d5d56819d8e39979c275` (`git worktree` at `$RUNNER_TEMP/baseline`, created in the run's "Create pinned baseline worktree" step) |
| Python | 3.12.3 |
| uv | 0.11.11 |
| mypy | 2.3.1 (compiled), from `uv.lock` |
| Pyrefly | 1.4.0 via `uv tool run pyrefly==1.4.0` (never in `uv.lock`) |
| Ruff | 0.16.2 |
| CPU / cores / memory | AMD EPYC 7763 64-Core Processor / 4 / 15989 MB |
| OS | Ubuntu 24.04.5 LTS, `Linux-6.17.0-1022-azure-x86_64-with-glibc2.39` |
| Workflow run | [39989820303](https://github.com/mctlhq/mctl-agents/actions/runs/39989820303) |

## Configuration (A and B)

- (A) auto-migrated: no Pyrefly config. Pyrefly prints
  ``No `pyrefly.toml` found — using settings imported from `[tool.mypy]` in
  your `pyproject.toml` (preset: legacy).`` Its own `dump-config` output is
  committed verbatim in `diagnostics/pyrefly-A-config-dump.txt`: 136 covered
  files, import root = the worktree, site packages queried from
  `.venv/bin/python`.
- (B) explicit: `docs/spikes/707/pyrefly.toml`, passed with `--config`
  (dump in `diagnostics/pyrefly-B-config-dump.txt`). It includes
  `orchestrator`, `config` and `tools`, sets Python 3.12, and puts the repo
  root and `tools/` on the search path (mypy resolves `tools/*.py` as
  top-level modules). It adds the venv site-packages. The mypy `tests.*`
  `ignore_errors` override has no counterpart: `tests/` is outside
  `project-includes`, as it is outside mypy's `files`. Nothing is stricter
  than mypy. Size: 10 lines, 6 of them settings.
- (C) stricter mode: not run (optional extra).

## Checked file sets

mypy (`-v` "Found source" lines), Pyrefly (A) and Pyrefly (B) each cover the
same 136 files. All four set differences in `results.json`
`diagnostics.file_sets` are empty.

## Latency

Cold: 10 fresh-process runs per checker, with the mypy cache removed before
each. Warm mypy: the incremental cache is kept, reported separately. Edit
loop: 20 apply/check/revert iterations per checker over mutations 01-07, with
the mypy cache primed once. Pyrefly has no persistent cache in CLI mode, so
its cold and edit numbers are the same kind of run.

| Metric | mypy | Pyrefly (A) | Pyrefly (B) |
| --- | --- | --- | --- |
| Cold median / p95 | 11.691 s / 11.765 s | 0.977 s / 0.984 s | 0.982 s / 1.103 s |
| Warm-cache median / p95 (mypy only, no edit) | 0.304 s / 0.307 s | n/a | n/a |
| Edit-loop median / p95 (20 iterations) | 0.346 s / 0.572 s | 0.970 s / 0.999 s | 0.973 s / 1.004 s |
| Edit iterations with an error on the mutated file | 20 / 20 | 20 / 20 | 20 / 20 |

Pyrefly is about 11.9x faster than a cold mypy run. Once mypy has its
incremental cache, every later check after an edit is about 2.8x faster with
mypy (0.35 s against 0.97 s median).

## Lint-job timing

One lint-job-equivalent in a fresh clone with an empty uv cache, phases timed
separately (`results.json` `lint_job`):

| Phase | Seconds |
| --- | --- |
| `uv sync --locked` | 2.136 |
| Ruff | 0.206 |
| mypy (cold) | 11.777 |
| Total of the timed phases | 14.119 |

This excludes checkout, `setup-uv` and the Python download, which the runner
does in both cases. Contrary to the `lint` job's comment, mypy, not setup,
is the largest timed phase on this runner (83 %). Replacing it would save
about 11 s per lint job. The decision criteria weigh repeated-check latency,
not this number (see Recommendation).

Install size: Pyrefly adds 71.3 MB to the uv download cache. A first fetch
took 0.406 s. mypy plus mypyc in the synced venv take 13.4 MB.

## Diagnostic comparison

mypy reports 0 diagnostics on the baseline (exit 0, `diagnostics/mypy.txt`).
Pyrefly reports 15 under (A) and 13 under (B), with 14 suppressed in both.
Every unmatched Pyrefly finding was read against the source at the pinned SHA.
No source was changed.

| Finding (B unless noted) | Count | Class | Evidence |
| --- | --- | --- | --- |
| `bad-argument-type` `Any \| None` -> `str` for `kind`/`name` (capability.py:818, context_snapshot.py:979, execution_evidence.py:1234, execution_identity.py:464, resolver.py:774) | 5 | false positive / noise | The value comes from `mapping.get(...)` and is rejected unless `!= expected_*` is false, where `expected_*` is a known `str`. Pyrefly does not narrow on that equality. Read at four sites; execution_evidence.py:1234 carries the same message shape. |
| `bad-argument-type` `str \| Any` -> `int` `schema_version` (commerce.py:332, 333) | 2 | false positive / noise | `DispatchAuthorization(**{**x.to_dict(), ...})`. `to_dict` is declared `-> dict[str, Any]`, and Pyrefly infers the value union from the dict literal instead. |
| `bad-argument-type` `strategy` `str \| None` (context_assembly.py:1713) | 1 | false positive / noise | Guarded by `resolution.bound_strategy not in (None, config.strategy)` at line ~1696. Pyrefly does not narrow on `not in` against a tuple containing `None`. |
| `unbound-name` `CapabilityGateway`, `PolicyDecidePolicyCheckpoint` (run_issue_investigator.py:2505, 2507) | 2 | false positive / noise | A deliberate lazy import (worker-isolation rule, ADR 017) under an earlier `if capability_mode == "discovery":`. Line 2505 runs only under the same condition. Absent from (A): the `legacy` preset disables the check. |
| `unsupported-operation` set item in `dict[str, str]` (tools/diagram_facts.py:117, 122, 123) | 3 | useful: annotation defect, no runtime bug | `_grep` is annotated `-> dict[str, str]`, but callers store `sorted(...)` lists into it. mypy misses this because `facts` is `dict[str, Any]`. One root cause: the annotation. |
| `missing-import` `check_binding_hash`, `publish_agent_release`-local modules (tools/check_agent_bindings.py:69-71, tools/publish_agent_release.py:95) | 4 (A only) | migration/config mismatch | (A) does not put `tools/` on the search path. Config (B) fixes it. |

mypy-only findings: none (mypy has zero). Result: 3 of the 13 (B) diagnostics
are useful, from one root cause of low severity, and 10 are noise. Adopting
Pyrefly as a gate would need 10 suppressions or config exceptions on day one.

## Mutation detection

Full table in `docs/spikes/707/mutations/README.md`. mypy, Pyrefly (A) and
Pyrefly (B) each catch patches 01-07 with an actionable file:line:cause
message from which the seeded defect can be repaired. All three miss patch 08
(wrong type through an unannotated parameter), the expected soft-mode `Any`
boundary. No checker caught anything the others missed.

## Suppression compatibility

Pyrefly honours all 10 existing `# type: ignore[...]` comments: neither (A)
nor (B) reports at those lines on the runner (`diagnostics/pyrefly-*.txt`).
The "sees the defect without it" column is from one local strip-test on a
scratch copy, not from the runner run.

| Site | Code | mypy | Pyrefly | Pyrefly sees the defect without it (local) |
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

Pyrefly does not mirror `warn_unused_ignores`. It does not flag the four
ignores in the "no" rows that it considers unnecessary. mypy stays the only
checker that enforces that setting.

## Library notes

Runner output of (B) has no import or stub error for `temporalio`, `mcp`,
`httpx`, `yaml`/`types-pyyaml` or `claude_agent_sdk`. All resolve from the
synced venv through the site-packages path. Mutations 06 and 07 in
`orchestrator/temporal/worker.py` show that Pyrefly types `temporalio` calls
precisely enough to flag an unawaited `create_schedule` and an `await` on
`list[Payload]`. The only import problem is the (A) `tools/` search-path
mismatch above.

## Inner-loop assessment

The candidate integration point is the "Service-specific notes" section of
`agents/mctl-agents/.claude/agents/implementer.md`, with this command:

    uvx pyrefly==1.4.0 check --config docs/spikes/707/pyrefly.toml

Pinning is by exact version and committed config. The production image is
built with `uv sync --locked --no-dev`, so a per-run `uvx` fetch costs 71.3 MB
and 0.406 s on this runner. Static feedback per seeded patch (checker, then
Ruff) in `results.json` `loop`, five patches:

| Sequence | Checker median | Ruff | Static feedback median |
| --- | --- | --- | --- |
| mypy (cache warmed on the clean tree) -> Ruff -> targeted pytest | 0.350 s | 0.018 s | 0.369 s |
| Pyrefly (B) -> Ruff -> targeted pytest | 0.984 s | 0.019 s | 1.003 s |

An implementer session pays one cold check, then a warm one per edit. With
Pyrefly that one check is 10.7 s faster on this runner. After that, every
check is about 0.6 s slower than warm mypy. The cold-start gap can be closed
without a second checker: run `uv run --locked mypy` once when the session
starts, so the cache is warm before the first edit.

## Metrics table

| Metric | mypy | Pyrefly |
| --- | --- | --- |
| Version | 2.3.1 | 1.4.0 |
| Exit code / diagnostics on baseline | 0 / 0 | 1 / 15 (A), 1 / 13 (B) |
| Files checked | 136 | 136 (A and B) |
| Cold median / p95 | 11.691 s / 11.765 s | 0.982 s / 1.103 s (B) |
| Edit-loop median / p95 | 0.346 s / 0.572 s | 0.973 s / 1.004 s (B) |
| Loop static feedback (checker + Ruff) | 0.369 s | 1.003 s (B) |
| Mutations caught (of 8) | 7 | 7 (A and B) |
| Unique useful findings | 0 | 1 root cause (3 diagnostics, tools/diagram_facts.py) |
| False-positive / noise findings | 0 | 10 (B) |
| Config / suppression migration issues | none | 6-setting config; 10 noise findings to suppress; no `warn_unused_ignores` equivalent |
| Lint-job timed phases | 14.119 s total, 11.777 s mypy | not measured as a gate (spike changes no gate) |
| Install size | 13.4 MB in venv | 71.3 MB uv cache |

## Recommendation

Recommendation: KEEP_MYPY

Per criterion in the issue:

- `REPLACE_MYPY` needs all five to hold. (1) holds: same 7 of 8 mutations
  caught, no blind spot found. (2) fails: 10 of the 13 extra diagnostics are
  noise from narrowing Pyrefly does not model (`!=`, `not in`, correlated
  conditions), so a gate would start with 10 suppressions. (3) fails:
  repeated checks are slower with Pyrefly (0.97 s against 0.35 s). (4) is not
  shown either way. (5) holds: the migration is a 6-setting config.
- `PYREFLY_INNER_LOOP_ONLY` needs Pyrefly to be "materially faster and
  useful for early feedback". It is faster only for the first check of a
  process with no mypy cache. It is slower for every check after that, which
  is the loop an implementer repeats. It catches nothing mypy misses among
  the seeded defects, and its one useful extra finding is a low-severity
  annotation defect.
- `KEEP_MYPY` applies: "a second checker adds complexity without changing
  DevLoop feedback latency in practice". A warm mypy cache gives the
  implementer faster repeated feedback than Pyrefly, with no new tool, config,
  or 71 MB download.

What this verdict does not cover: a stricter Pyrefly mode (C), Pyrefly as a
language server, and the 11 s cold-CI saving, which no criterion here weighs.
The `tools/diagram_facts.py` `_grep` annotation can be fixed on its own,
without Pyrefly. The owner records the verdict on the issue; this report only
recommends.
