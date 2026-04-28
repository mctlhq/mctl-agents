# Architecture: mctl-api

## Purpose
Central platform API: REST endpoints for UI/CLI/agents + MCP server (Streamable HTTP) for Claude/AI clients. Entry point `https://api.mctl.ai`. Serves the `admins` tenant, talks to the Kubernetes API and Vault for orchestration.

## Tech stack
- **Go 1.24**, modules
- **chi/v5 5.2.1** — HTTP router
- **httprate 0.15** — rate limiting middleware
- **mark3labs/mcp-go 0.31** — MCP server (24 tools, Streamable HTTP transport)
- **pgx/v5 5.8** — Postgres driver (identities, audit logs)
- **client-go 0.32** — Kubernetes API client
- **go-oidc/v3** — OIDC JWT verification (Dex / GitHub OAuth)
- **prometheus/client_golang 1.23** — metrics at /metrics
- **uuid 1.6**, **yaml.v3** for parsing manifests

## Auth flow (3 bearer types)
- **GitHub PAT** (no dots) → mctl-api → GitHub API → org membership → tenant groups from gitops
- **Dex JWT** (iss != self) → JWKS at ops.mctl.me/api/dex/keys → groups from claims
- **OAuth JWT** (iss == self) → HMAC-SHA256 verification → GitHub API for groups
- `AUTH_REQUIRED=false` locally

## MCP server
- Endpoint: `https://api.mctl.ai/mcp` (Streamable HTTP, POST + GET)
- 24 tools (11 read + 13 write — `get_service_status`, `get_tenant_metrics`, `list_incidents`, `trigger_workflow`, `get_workflow_logs`, identity tools, etc.)
- OAuth 2.0 PKCE for the Claude.ai connector
- Auth header per request

## External integrations
- **Vault** (`secrets.mctl.ai`) — service secrets via auth/kubernetes
- **ArgoCD** (`ARGOCD_TOKEN` from Vault) — application status
- **Backstage API** (`BACKSTAGE_TOKEN` from Vault) — service catalog, scaffolder triggers
- **Argo Workflows** (`workflows.mctl.ai`) — submit/inspect runs
- **Postgres** — tenant data, audit logs
- **Kubernetes API** — pods/services/cronjobs/workflows status

## Dependencies for researcher
Researcher tracks:
- `golang/go` — major LTS bumps (we are on 1.24)
- `go-chi/chi` — security/perf in the HTTP router
- `mark3labs/mcp-go` — critical library for the MCP server
- `jackc/pgx` — driver for Postgres
- `kubernetes/client-go` — major releases (current 0.32 ↔ k8s 1.32)
- `coreos/go-oidc` — JWT validation
- `prometheus/client_golang`
- `argoproj/argo-cd` — for status compatibility
- `argoproj/argo-workflows` — CRD/API changes
- CVEs across all of the above via GHSA + WebSearch

## Known limitations
- All MCP write tools must check the requester's org membership
- `pgx` connection pool tuning depends on prod load
- mctl-api itself authorizes tenant scope — a bug here = cross-tenant leak

## What NOT to do (for analyst)
- Do not propose switching the Go router to gin/echo without a strong benchmark
- Do not propose replacing pgx with an ORM (gorm) — we lose control over queries
- Do not propose moving MCP to gRPC — Claude.ai clients require HTTP
