# Model policy providers

A profile in `config/model-policy.yaml` may declare an optional `provider`
block to route sessions to a non-Anthropic endpoint that speaks the Anthropic
Messages API (spike mctlhq/mctl-agents#791, issue #824). The policy stays
`version: 1`; profiles without the block behave exactly as before.

```yaml
profiles:
  experimental_hetzner:
    model: Qwen/Qwen3.6-35B-A3B-FP8
    provider:
      name: hetzner                      # [a-z0-9-]+, used in logs/plan
      api: anthropic-messages            # only accepted value
      base_url: https://inference.hetzner.com/api
      credential_env: HETZNER_INFERENCE_API_KEY
      pin_model_slots: true              # must be true
      fallback: none                     # must be none
```

## Validation (fails at policy load with `ModelPolicyError`)

- Unknown or missing keys; `api` other than `anthropic-messages`.
- `base_url` must be an absolute `https://` URL with a host and no
  credentials, query or fragment (loopback `http://` is rejected).
- `credential_env` must be an upper-case env var name and must not be an
  Anthropic credential (`ANTHROPIC_*`, `CLAUDE_CODE_OAUTH_TOKEN*`).
- `pin_model_slots` must be `true`, `fallback` must be `none`, and the
  profile must not set `model_env`. A non-empty legacy env override for a
  task routed to a provider profile raises instead of overriding the model.

## Session env

`config.model_policy.provider_session_env` derives the child env from the
policy: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` (the value of
`credential_env`), all six model slots pinned to the model, and
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`. Every `ANTHROPIC_*` and
`CLAUDE_CODE_OAUTH_TOKEN*` value and the raw `credential_env` are blanked
over the effective env (process env overlaid with the builder env).
`orchestrator.options._scrubbed(options, provider=...)` applies it and sets
`CLAUDE_CONFIG_DIR` to an empty per-session directory. An unset or empty
credential raises; there is no fallback to Anthropic.

## Fail-closed behaviour

`config/settings.py` exposes plain model strings, which cannot carry an
endpoint. If any module-level selection (`SERVICE_AGENT_SELECTION`,
`MENTOR_SELECTION`, `SHEPHERD_SELECTION`, `SCOUT_*_SELECTION`) resolves to a
provider profile, `config.settings` raises `ModelPolicyError` at import. That
stops the whole worker process, not one task; this is intended.

## Adoption precondition

No committed profile declares a provider. Before any task is routed to one,
the provider session must blank `GITHUB_TOKEN`/`GH_TOKEN` or receive a
read-only token; this change does not alter their handling.
