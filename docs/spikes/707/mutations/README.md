# Mutation corpus (#707)

Patch files against the pinned baseline SHA
`68f10afcb8a96d7b1c54d5d56819d8e39979c275`. They are **never applied on a
merged branch**: `bench.py` applies each in a clean `git worktree` and reverts
it in a `finally` block. Each is a one-to-three line change on real code.

| Patch | Defect class | Source pattern (file, function) |
| --- | --- | --- |
| 01-optional-to-nonoptional | `str \| caught (`bad-argument-type`, `missing-attribute`) | caught |
| 02-incompatible-assignment | `str` assigned to an `int`-annotated name | caught (`bad-assignment`) | caught 
| 03-wrong-argument-type | `int` passed where `str` is declared | caught (`bad-argument-type`) | caught 
| 04-wrong-return-type | `int` returned from a `-> str` function | caught (`bad-return`) | caught 
| 05-attribute-on-optional | attribute access on `str \| caught (`missing-attribute`) | caught |
| 06-unawaited-coroutine | coroutine created and dropped | caught (`unused-coroutine`) | caught 
| 07-await-non-awaitable | `await` on a `list` | caught (`not-async`) | caught 
| 08-any-boundary | wrong type across an unannotated parameter (expected to be missed by both: soft-mode `Any` boundary) | `ci_checks._fetch_one_log`: `cwd=42` into `proc.run_capturing(cwd=None)` |

## Detection table

Detection result from the linked `spike-707-bench` run
[39987958862](https://github.com/mctlhq/mctl-agents/actions/runs/39987958862)
(`results.json` `mutations`; mypy with a primed cache, detection only, no
timings). "Actionable" means the message names the file, the line and the
cause. Pyrefly codes are the same in (A) and (B).

| Patch | mypy | Pyrefly (A) | Pyrefly (B) | Actionable / repairable from diagnostic |
| --- | --- | --- | --- | --- |
| 01 | caught (`arg-type`, `union-attr`) | caught (`bad-argument-type`, `missing-attribute`) | caught | yes / yes |
| 02 | caught (`assignment`) | caught (`bad-assignment`) | caught | yes / yes |
| 03 | caught (`arg-type`) | caught (`bad-argument-type`) | caught | yes / yes |
| 04 | caught (`return-value`) | caught (`bad-return`) | caught | yes / yes |
| 05 | caught (`union-attr`) | caught (`missing-attribute`) | caught | yes / yes |
| 06 | caught (`unused-coroutine`) | caught (`unused-coroutine`) | caught | yes / yes |
| 07 | caught (`misc`) | caught (`not-async`) | caught | yes / yes |
| 08 | missed | missed | missed | n/a (neither checker sees it) |

Raw diagnostics per patch are in `results.json` under `mutations`.
