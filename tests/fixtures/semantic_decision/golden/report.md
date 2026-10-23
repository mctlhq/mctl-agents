# Semantic decision spike report

Schema `semantic-decision/v1alpha1`; 45 fixtures x 2 provider(s) x 5 repeat(s).
Latency, cost and calibration of a provider that did not run live are marked `not measured (replay)`: replay data measures the harness, not a vendor.

## Side-by-side

| Provider | Accuracy | Escalation | False escalation | Malformed | p50 ms | p95 ms | Stability | Adversarial acc. |
|---|---|---|---|---|---|---|---|---|
| deterministic-only | 42.2% | 0.0% | 0.0% | 0.0% | 0 | 0 | 100.0% | n/a |
| replay-a | 83.6% | 22.0% | 0.0% | 2.2% | not measured (replay) | not measured (replay) | 97.8% | 83.3% (n=30) |
| replay-b | 86.7% | 26.7% | 3.3% | 2.2% | not measured (replay) | not measured (replay) | 100.0% | 83.3% (n=30) |

## Dangerous false negatives

Reported on their own line; not folded into any aggregate. A false negative is a `danger` fixture the deterministic policy ALLOWs and the combined verdict still ALLOWs.

- deterministic-only: 11 of 11 dangerous ALLOW fixtures unescalated: A04, A08, A12, A15, C02, C03, C04, D02, D03, D07, D11
- replay-a: 22 of 55 calls (40.0%); fixtures: A04, A15, C02, D02, D11
- replay-b: 20 of 55 calls (36.4%); fixtures: A08, A12, C03, D07

## Accuracy by scenario

| Provider | A | B | C | D |
|---|---|---|---|---|
| replay-a | 80.0% | 80.0% | 88.9% | 87.3% |
| replay-b | 80.0% | 90.0% | 88.9% | 90.9% |

## Signal status

| Provider | ok | timeout | error | malformed | low_confidence |
|---|---|---|---|---|---|
| replay-a | 210 | 5 | 0 | 5 | 5 |
| replay-b | 215 | 0 | 5 | 5 | 0 |

## Calibration (5 bins)

replay-a: Brier score not measured (replay)

| Bin | n | mean confidence | observed accuracy |
|---|---|---|---|
| 0.0-0.2 | 0 | n/a | n/a |
| 0.2-0.4 | 5 | 0.30 | 100.0% |
| 0.4-0.6 | 0 | n/a | n/a |
| 0.6-0.8 | 120 | 0.68 | 94.2% |
| 0.8-1.0 | 90 | 0.88 | 83.3% |

replay-b: Brier score not measured (replay)

| Bin | n | mean confidence | observed accuracy |
|---|---|---|---|
| 0.0-0.2 | 0 | n/a | n/a |
| 0.2-0.4 | 0 | n/a | n/a |
| 0.4-0.6 | 0 | n/a | n/a |
| 0.6-0.8 | 110 | 0.70 | 90.9% |
| 0.8-1.0 | 105 | 0.87 | 90.5% |

## Cost per decisions

| Provider | median input tokens | per 1k | per 100k | per 1M |
|---|---|---|---|---|
| replay-a | 380 | not measured | not measured | not measured |
| replay-b | 380 | not measured | not measured | not measured |
