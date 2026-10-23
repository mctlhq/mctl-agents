# portable-runtime-rs (EXPERIMENTAL, default-off)

Spike for mctlhq/mctl-agents#746. Slice 1 (#759) scaffolded the crate; slice 2
(#764) adds a stdio-to-Streamable-HTTP MCP bridge, credential handling,
per-call correlation, `init` and `doctor`, and an offline fake upstream for
`cargo test`. It is not built into the container image, not imported by the
orchestrator, excluded by `.dockerignore`, and guarded by
`tests/test_portable_runtime_isolation.py`.

## Subcommands

    mctl-runtime proxy  [--upstream URL] [--allowlist PATH] [--timeout-ms N]
    mctl-runtime init   --host fixture|claude-code [--token-file PATH] [--upstream URL]
                        [--write --config-dir DIR]
    mctl-runtime doctor [--upstream URL] [--allowlist PATH] [--timeout-ms N]
    mctl-runtime --version

- `proxy` serves newline-delimited JSON-RPC on stdio (`initialize`, `ping`,
  `tools/list`, `tools/call`) in front of one upstream `/mcp` endpoint.
  Defaults: `--upstream https://api.mctl.ai/mcp`, `--allowlist
  fixtures/allowlist.toml`, `--timeout-ms 10000`. It exits 2 before serving if
  the allowlist is missing, empty or invalid. `tools/list` returns only
  allowlisted upstream tools, verbatim. A non-allowlisted `tools/call` returns
  `isError: true` with reason `not-eligible` and makes no upstream request.
  Allowlisted results, including upstream refusals, pass through unchanged.
  Failures map to `provider-unavailable`, `provider-timeout`,
  `credential-rejected` (HTTP 401/403, one attempt) and `credential-absent`
  (no request at all); the session keeps answering `ping`.
- Each `tools/call` writes one JSON line to stderr with `invocation_id`,
  `host_request_id`, `tool`, `args_sha256`, `upstream_session_id`,
  `upstream_request_id`, `outcome`, `is_error`, `latency_ms`. The same
  `invocation_id` is sent upstream as `X-Mctl-Invocation-Id`. `args_sha256` is
  `sha256:` plus the digest of the canonical JSON of the arguments (the rule of
  `orchestrator/context_snapshot.py::_canonical_json`). No argument value,
  result value or token is logged, and nothing is written to disk.
- `init` prints host config to stdout (dry run). `--write` needs `--config-dir`
  and refuses real host config locations under `$HOME` (`.claude`,
  `.claude.json`, `.config/claude`, `Library/Application Support/Claude`,
  `.cursor`). The config references the credential by env var name or file path.
- `doctor` prints one `check: status detail` line each for version, credential
  source, token file mode, allowlist count, the upstream round trip and
  allowlisted tools missing upstream, and exits non-zero on any failure.

## Credentials

The bearer token comes from `MCTL_RUNTIME_TOKEN_FILE` (a file with no group or
other permission bits, `0600`) or, if that is unset, `MCTL_TOKEN`. No flag
accepts a token. Child processes are built only by `src/spawn.rs`, which removes
both variables from the child environment. Keychain and loopback sources exist
as stubs that return `Unsupported`.

## Design choices and limits

- **std-only.** The crate has no registry dependency and `Cargo.lock` holds only
  the package itself, so `cargo build --locked` and `cargo audit` cannot fail on
  lockfile or advisory problems. JSON, SHA-256, UUIDv4 and HTTP/1.1 are
  hand-rolled (each with unit tests). `rmcp` was not used because the offline
  implementer cannot produce checksummed lock entries for it.
- **MCP subset.** Hand-rolled `initialize`, `notifications/initialized`,
  `tools/list`, `tools/call` and `ping`. Streamable HTTP only; the legacy MCP
  SSE transport is not implemented. A `text/event-stream` response to a POST is
  accepted as the body form.
- **http-only upstream.** The upstream client speaks plain `http://`. An
  `https://` upstream, including the real `api.mctl.ai/mcp`, is refused with
  `provider-unavailable` ("https upstream requires TLS (deferred, see README)")
  until a later slice adds TLS. A live `doctor` round trip is therefore not
  possible yet.
- `upstream_request_id` is taken from the `X-Request-Id` response header and
  `upstream_session_id` from `Mcp-Session-Id`; both are `null` when absent.
- Canonical JSON keeps number text as received, so a number such as `1.0` is not
  rewritten the way Python would rewrite a parsed float.

## Build and test

    cd experiments/portable-runtime-rs
    cargo build --locked
    cargo test --locked

`cargo test` is offline: the fake upstream binds `127.0.0.1:0`. CI verification
lives in `.github/workflows/portable-runtime-spike.yml` (optional, not a
required check).

## Delete

    git rm -r experiments/portable-runtime-rs .github/workflows/portable-runtime-spike.yml

The isolation test then skips itself.
