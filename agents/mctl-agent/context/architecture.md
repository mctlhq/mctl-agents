# Architecture: mctl-agent

## Purpose
Self-healing GitOps agent. Receives alerts from AlertManager (webhook + periodic poll), diagnoses via Claude API + builtin Go skills, opens a fix PR in `mctlhq/mctl-gitops`. Runs in the `admins` tenant, `https://agent.mctl.ai`.

## Tech stack
- **Go 1.24**
- **chi/v5 5.2.1** — HTTP router (REST API + AlertManager webhook + Telegram webhook)
- **google/go-github v68** — GitHub PR creation
- **modernc.org/sqlite 1.34** — pure Go SQLite (no CGO) for tickets DB and skill metrics
- **uuid 1.6**
- **slog** — structured logging
- Anthropic SDK (vendored or direct HTTP calls) — for diagnose phase

## Architecture (3-tier skill system)
1. **Builtin Go skills** (`internal/skill/builtin/`): 9 total — OOMKilled, ImagePull, Rollback, ArgoCDDrift, ProbeFix, CPUThrottle, QuotaAdjust, ScaleUp, LLMDiagnosis
2. **YAML skills** (`skills/custom/`): hot-reload without restart, for in-place team commands
3. **Remote skills** (HTTP): registered via REST API, delegate diagnosis to an external service

Pipeline: ticket → evidence → skill match (ranked by confidence + circuit breaker) → diagnose → fix → PR → notify.

## API endpoints
- `POST /api/v1/alerts` — AlertManager webhook
- `POST /api/v1/telegram` — Telegram bot webhook
- `GET /api/v1/tickets` / `GET /api/v1/skills` / `POST /api/v1/skills/register`
- `POST /mcp` — MCP JSON-RPC endpoint (6 tools)
- `GET /healthz` / `/readyz`

## Known routed alerts
**PR-capable:** PodCrashLooping, KubePodCrashLooping, KubePodNotReady, PodNotReady, TenantCPUQuotaHigh, TenantMemoryQuotaHigh, ArgoWorkflowFailed, ArgoWorkflowHighFailureRate
**Diagnose-only:** CPUThrottlingHigh, KubeJobNotCompleted, KubePersistentVolumeFillingUp, KubeStatefulSetReplicasMismatch

## External integrations
- **GitHub App** — installation token rotated every 30 minutes via CronWorkflow `cwft-rotate-github-token`. Vault path: `secret/platform/github-app`
- **Anthropic API** — Claude for diagnose
- **mctl-gitops** — destination for fix PRs
- **Telegram Bot** — notifications and interactive ack/reject

## Dependencies for researcher
- `golang/go` — major LTS bumps
- `go-chi/chi`
- `google/go-github` — security/perf
- `anthropics/anthropic-sdk-go` (if used) — major bumps
- `modernc.org/sqlite` — security
- AlertManager (`prometheus/alertmanager`) — webhook contract changes
- ArgoCD CRDs — may change Application spec
- Argo Workflows CRDs
- CVEs against any of the above

## Known specifics
- **Circuit breaker** on skill metrics — auto-disables after N consecutive fails. For long-unavailable routines — manual re-enable
- **Stale ticket gate** — recent fix (94f1bb0) — auto-resolve by heartbeat type, not by generic refresh
- All Go skills — table-driven tests (rule from CLAUDE.md)

## What NOT to do (for analyst)
- Do not propose switching SQLite to Postgres — single-pod design, SQLite is fine
- Do not propose removing LLMDiagnosis — fallback skill, important
- Do not propose handling ALL AlertManager alerts — only routed ones; new ones are a separate proposal
- Do not touch circuit breaker thresholds without real prod data
