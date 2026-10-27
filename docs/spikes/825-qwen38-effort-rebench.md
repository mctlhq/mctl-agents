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

Pending the operator run.
