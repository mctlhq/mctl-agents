# Spike 917 record schema

`fixture-records.jsonl` is synthetic. Real artefacts must hold no token
values, prompts or completions.

One JSON object per line (one agent stage run):

- `run_id`, `window` (key into the manifest `windows`), `stage`, `model`,
  `effort`, `turns`, `output_tokens`, `ended_at` (epoch seconds)
- `outcome`: `completed` or `rate_limited` (others are ignored for throughput)
- `calculated_cost_usd` and `pricing_version`: list-price estimate only
  (ADR-012 `calculated_cost`); never a provider charge
- `rate_limit_events`: list of `{status, rate_limit_type, lost_s, ts}` with
  `status` one of `allowed`, `allowed_warning`, `rejected`. A missing
  `rate_limit_type` is reported as `unknown`.

Manifest (`fixture-manifest.json`): `windows` (`{start,end}` epoch seconds),
`first_five_hour_boundary_ts`, `sources` (`{observed, reason}`), and
`weekly_samples` (`{ts, all_models_remaining_pct, fable_remaining_pct}`,
recorded by hand from the provider usage page). Usage-credit spend is not
represented here; it comes only from the provider billing surface.
