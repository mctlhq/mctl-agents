# 825: Qwen3.8-27B re-bench with policy-declared reasoning effort

Origin: #791 found that Hetzner `Qwen/Qwen3.8-27B` rejects the Claude CLI default effort `high`
(HTTP 400). The effort is now declared per profile in the model policy (`effort:`), applied by the
session builder, and the #791 harness takes the candidate model and effort from a policy file.

Bench policy: `docs/spikes/825/model-policy-qwen38.yaml` (profile `experimental_hetzner_qwen38`,
`effort: medium`, task `bench_candidate`). The committed `config/model-policy.yaml` is unchanged.

## Operator steps (live, credentials from your own keychain)

```
export HETZNER_INFERENCE_BASE_URL=https://inference.hetzner.com/api
export HETZNER_INFERENCE_API_KEY=...   # from your keychain
python tools/hetzner_inference_spike.py snapshot --allow-live --out docs/spikes/825/snapshot.json
python tools/hetzner_inference_spike.py bench --allow-live --arm candidate \
  --out-dir docs/spikes/825 --snapshot docs/spikes/825/snapshot.json \
  --model-policy docs/spikes/825/model-policy-qwen38.yaml --policy-task bench_candidate
python tools/hetzner_inference_spike.py report --exploratory docs/spikes/825
```

Commit `snapshot.json`, `records-candidate.jsonl` and `run-manifest-candidate.json` in a follow-up PR
that references #825.

Acceptance: every candidate record has `effort: medium` and `effort_source: policy`, and the HTTP 400
count (`count_http_400`, printed by the exploratory report) is 0.

## Result

**Acceptance: met.** All 12 candidate cells ran `Qwen/Qwen3.8-27B` with `effort: medium`,
`effort_source: policy`, and the HTTP 400 count is **0** (#791's main run had 24/24 HTTP 400s for this model
at the CLI default effort `high`). The effort reached the CLI through the model policy and the production
`apply_effort` seam, not through a harness flag.

This run is exploratory. It does not feed the pre-registered #791 `decide()` and does not change the #791
decision token `ADOPT_EXPERIMENTAL_PROVIDER`.

Setup: the same harness, fixtures, session builders, hooks and container isolation as the #791 candidate arm
(throwaway Docker container, `python:3.12-slim`, linux/arm64, on the owner's workstation; git-archive export
of the run commit; no host mounts except the output directory; no credential except the Hetzner key, piped
from the keychain and never written to disk). Bench policy `docs/spikes/825/model-policy-qwen38.yaml`
(sha256 `1564ac80321b8184ec94074ed1c08f1df254f61c51d4b1f32e0f06930d12ff68`). Catalog snapshot taken with
`--deep` just before the run. Only the slash-form ID `Qwen/Qwen3.8-27B` is benched: the policy names one
model, and the harness refuses any substitution.

- run: `20261027T110259Z`, repo sha `66770074688930166e3978112495cb483743d567`, window (UTC) 2026-10-27T11:02:59Z to 2026-10-27T11:11:38Z
- candidate reasoning effort: `medium` (source: `policy`); everything else as in the main run (same builders, hooks, fixtures, container isolation)
- catalog at start (2026-10-27T11:01:41Z): `Qwen/Qwen3.6-35B-A3B`, `Qwen/Qwen3.6-35B-A3B-FP8`, `Qwen/Qwen3.8-27B`, `Qwen3.8-27B`

| model | investigation | implementation | review | tool-use |
|---|---|---|---|---|
| `Qwen/Qwen3.8-27B` | 3/3; 21 (19-30) | 3/3; 28 (26-34) | 0/3; 66 (48-70) | 2/3; 51 (41-81) |

| model | task | rep | status | oracle | turns | tokens in/out | tools | finish |
|---|---|---|---|---|---|---|---|---|
| `Qwen/Qwen3.8-27B` | investigation | 1 | ok | pass | 6 | 10785/485 | Bash 2, Grep 1, Read 2 | end_turn |
| `Qwen/Qwen3.8-27B` | investigation | 2 | ok | pass | 6 | 12105/870 | Bash 2, Grep 1, Read 2 | end_turn |
| `Qwen/Qwen3.8-27B` | investigation | 3 | ok | pass | 6 | 9700/765 | Grep 3, Read 2 | end_turn |
| `Qwen/Qwen3.8-27B` | implementation | 1 | ok | pass | 7 | 14047/862 | Bash 3, Edit 1, Read 2 | end_turn |
| `Qwen/Qwen3.8-27B` | implementation | 2 | ok | pass | 8 | 17008/930 | Bash 4, Edit 1, Read 2 | end_turn |
| `Qwen/Qwen3.8-27B` | implementation | 3 | ok | pass | 7 | 14181/825 | Bash 3, Edit 1, Read 2 | end_turn |
| `Qwen/Qwen3.8-27B` | review | 1 | ok | fail | 3 | 7456/1418 | Glob 1, Read 1 | end_turn |
| `Qwen/Qwen3.8-27B` | review | 2 | ok | fail | 4 | 9418/1254 | Bash 1, Glob 1, Read 1 | end_turn |
| `Qwen/Qwen3.8-27B` | review | 3 | ok | fail | 4 | 8044/1593 | Bash 1, Glob 1, Read 1 | end_turn |
| `Qwen/Qwen3.8-27B` | tool-use | 1 | ok | fail | 7 | 11939/1111 | Bash 3, Grep 1, Read 2 | end_turn |
| `Qwen/Qwen3.8-27B` | tool-use | 2 | ok | pass | 7 | 11662/1054 | Bash 3, Glob 1, Read 2 | end_turn |
| `Qwen/Qwen3.8-27B` | tool-use | 3 | ok | pass | 7 | 11856/1106 | Bash 3, Glob 1, Read 2 | end_turn |

| check | `Qwen/Qwen3.8-27B` |
|---|---|
| long context (~60k tokens) accepted | pass |
| tool-call output well formed | pass |
| reasoning does not exhaust output budget | pass |
| stop reasons reported | pass |
| recovers after a tool error | pass |
| complies with read-only instruction | pass |

- `Qwen/Qwen3.8-27B`: 12 cells, provider_error 0, HTTP 429 0, timeouts 0, policy bypasses 0, policy denials 0, HTTP 400 0

### Compared with the #827 flag run (`--effort medium`, 2026-10-26)

| | #827 flag run, `Qwen/Qwen3.8-27B` | this run (policy) |
|---|---|---|
| effort source | harness `--effort` flag | model policy, `apply_effort` |
| cells / provider errors / HTTP 400 | 12 / 0 / 0 | 12 / 0 / 0 |
| investigation | 3/3 | 3/3 |
| implementation | 3/3 | 3/3 |
| review | 0/3 | 0/3 |
| tool-use | 3/3 | 2/3 |
| model checks | 6/6 pass | 6/6 pass |

Findings:
- The policy path behaves like the flag path: no provider errors, no HTTP 400, all six model checks pass.
  `medium` is a working pin for `Qwen/Qwen3.8-27B`.
- Review still fails its oracle in every rep, as in #791 and #827. That is a quality limit of the model
  on this fixture, not an effort or transport problem.
- Tool-use passed 2/3 here against 3/3 in #827. With three reps per cell that difference is within noise;
  the failing rep finished normally (`end_turn`) with well-formed tool calls.

Artifacts: `docs/spikes/825/snapshot.json`, `records-candidate.jsonl`, `run-manifest-candidate.json`. All
three passed the harness credential screen (`contains_credential`).
