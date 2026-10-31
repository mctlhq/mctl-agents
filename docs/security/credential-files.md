# Agent credentials: file-backed readers

Runtime contract for mctlhq/mctl-agents#807 G2, implemented by
`orchestrator/credential_files.py`. The pod-manifest side is
mctlhq/mctl-gitops#2288.

Every credential below is read from `<NAME>_FILE` (a mounted Secret volume)
when that variable is set, at the credentialed operation. If the file is
unreadable, empty or not valid UTF-8, the last good value read from it in this
process is used, then the `<NAME>` env value (transitional fallback), then the
credential counts as absent. A failed read logs only the variable name, the
path and the exception type name. Only names, consumers and lifetime classes
are recorded here, never a value.

## Inventory

| Variable | File variable | Consumers | Binding | Class | Lifetime |
|---|---|---|---|---|---|
| `MCTL_TOKEN` | `MCTL_TOKEN_FILE` | REST clients (`action_approvals`, `commerce`, `lifecycle/client`, `lifecycle/claim`, `temporal/mctl_client`, `run_shepherd`, `pr_adoption`, `run_token`); MCP header in `options.mctl_mcp_config` and `capability_gateway` | per request; per SDK session for the MCP header | in-process | long-lived, operator-rotated (exact lifetime: not observed) |
| `MCTL_USAGE_WRITER_TOKEN` | `MCTL_USAGE_WRITER_TOKEN_FILE` | `usage_ledger`, `run_usage_collector` | per delivery | in-process | long-lived, operator-rotated (exact lifetime: not observed) |
| `MCTL_EVIDENCE_WRITER_TOKEN` | `MCTL_EVIDENCE_WRITER_TOKEN_FILE` | `evidence_producer`, `decision_records` | per call | in-process | long-lived, operator-rotated (exact lifetime: not observed) |
| `SCOUT_TELEGRAM_BOT_TOKEN` | `SCOUT_TELEGRAM_BOT_TOKEN_FILE` | `run_scout` | per run | in-process | long-lived, operator-rotated (exact lifetime: not observed) |
| `GITHUB_TOKEN` | `GITHUB_TOKEN_FILE` | `gh`/`git` children | per subprocess, via `refresh_github_token()` and once at each entrypoint start | subprocess-env | short-lived (GitHub App token, kubelet-refreshed file) |
| `CLAUDE_CODE_OAUTH_TOKEN` | `CLAUDE_CODE_OAUTH_TOKEN_FILE` | `auth.session_auth_env` | per SDK session | cli-env | long-lived, operator-rotated (exact lifetime: not observed) |
| `CLAUDE_CODE_OAUTH_TOKEN_SECONDARY` | `CLAUDE_CODE_OAUTH_TOKEN_SECONDARY_FILE` | `auth.rotate_to_secondary_auth` | per SDK session, after a 429 | cli-env | long-lived, operator-rotated (exact lifetime: not observed) |
| `ANTHROPIC_API_KEY` | `ANTHROPIC_API_KEY_FILE` | `auth.session_auth_env` | per SDK session | cli-env | long-lived, operator-rotated (exact lifetime: not observed) |
| `ANTHROPIC_API_KEY_SECONDARY` | `ANTHROPIC_API_KEY_SECONDARY_FILE` | `auth.rotate_to_secondary_auth` | per SDK session, after a 429 | cli-env | long-lived, operator-rotated (exact lifetime: not observed) |

In-process credentials are never written to `os.environ`, to any
`ClaudeAgentOptions.env`, to argv or to logs. `_scrubbed()` in
`orchestrator/options.py` blanks both `<NAME>` and `<NAME>_FILE` for each of
them in every SDK session env. The Anthropic credential is put into a
session's `options.env` only (single active variable), never `os.environ`.
`GITHUB_TOKEN` keeps being written to `os.environ` by `refresh_github_token()`
because the `gh`/`git` children need it.

## Contract for mctl-gitops#2288

- Seeds that can be removed once this runtime is deployed, one credential at
  a time: the `secretKeyRef` env entries for every variable in the table,
  replaced by a Secret volume mount and `<NAME>_FILE`.
- Rolling back the image after a seed was removed breaks that consumer:
  restore the seed first.
- CWFT shell lines that test `$GITHUB_TOKEN` or `$CLAUDE_CODE_OAUTH_TOKEN`
  (the preambles before `gh auth setup-git`) must switch to
  `[ -s "$GITHUB_TOKEN_FILE" ]` / `[ -s "$CLAUDE_CODE_OAUTH_TOKEN_FILE" ]` in
  the same commit that removes the seed. The notify-telegram exit handlers
  (`TG_BOT_TOKEN`, `MCTL_TOKEN` curl calls) are shell, not covered by this
  runtime.

## Residual boundary

A credential file in a filesystem readable by model Bash, and any credential a CLI child needs in its env, is obtainable by the model shell; mctlhq/mctl-agents#869 owns that boundary.
