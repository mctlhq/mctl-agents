# Architecture: mctl-agent

## Назначение
Self-healing GitOps agent. Принимает алёрты из AlertManager (webhook + periodic poll), диагностирует через Claude API + builtin Go skills, открывает PR с fix'ом в `mctlhq/mctl-gitops`. Работает в `admins` тенанте, `https://agent.mctl.ai`.

## Технологический стек
- **Go 1.24**
- **chi/v5 5.2.1** — HTTP router (REST API + AlertManager webhook + Telegram webhook)
- **google/go-github v68** — GitHub PR creation
- **modernc.org/sqlite 1.34** — pure Go SQLite (без CGO) для tickets DB и skill metrics
- **uuid 1.6**
- **slog** — structured logging
- Anthropic SDK (вендорный или прямые HTTP вызовы) — для diagnose phase

## Архитектура (3-tier skill system)
1. **Builtin Go skills** (`internal/skill/builtin/`): 9 шт — OOMKilled, ImagePull, Rollback, ArgoCDDrift, ProbeFix, CPUThrottle, QuotaAdjust, ScaleUp, LLMDiagnosis
2. **YAML skills** (`skills/custom/`): hot-reload без restart'а, для команд на месте
3. **Remote skills** (HTTP): регистрация через REST API, делегируют диагноз внешнему сервису

Pipeline: ticket → evidence → skill match (ranked by confidence + circuit breaker) → diagnose → fix → PR → notify.

## API endpoints
- `POST /api/v1/alerts` — AlertManager webhook
- `POST /api/v1/telegram` — Telegram bot webhook
- `GET /api/v1/tickets` / `GET /api/v1/skills` / `POST /api/v1/skills/register`
- `POST /mcp` — MCP JSON-RPC endpoint (6 тулз)
- `GET /healthz` / `/readyz`

## Известные routed alerts
**PR-capable:** PodCrashLooping, KubePodCrashLooping, KubePodNotReady, PodNotReady, TenantCPUQuotaHigh, TenantMemoryQuotaHigh, ArgoWorkflowFailed, ArgoWorkflowHighFailureRate
**Diagnose-only:** CPUThrottlingHigh, KubeJobNotCompleted, KubePersistentVolumeFillingUp, KubeStatefulSetReplicasMismatch

## Внешние интеграции
- **GitHub App** — installation token rotated каждые 30мин через CronWorkflow `cwft-rotate-github-token`. Vault path: `secret/platform/github-app`
- **Anthropic API** — Claude для diagnose
- **mctl-gitops** — куда пушат fix-PR'ы
- **Telegram Bot** — нотификации и interactive ack/reject

## Dependencies для researcher
- `golang/go` — major LTS bumps
- `go-chi/chi`
- `google/go-github` — security/perf
- `anthropics/anthropic-sdk-go` (если используем) — major bumps
- `modernc.org/sqlite` — security
- AlertManager (`prometheus/alertmanager`) — webhook contract changes
- ArgoCD CRDs — могут менять Application spec
- Argo Workflows CRDs
- CVE по перечисленным выше

## Известные особенности
- **Circuit breaker** на skill metrics — auto-disable при N подряд fails. Для долго недоступных рутин — manual re-enable
- **Stale ticket gate** — реcent fix (94f1bb0) — auto-resolve по heartbeat type, не по generic refresh
- Все Go skills — table-driven tests (правило из CLAUDE.md)

## Что НЕ делать (для analyst)
- Не предлагать смену SQLite на Postgres — single-pod дизайн, SQLite норм
- Не предлагать удаление LLMDiagnosis — fallback skill, важен
- Не предлагать обработку ВСЕХ AlertManager алёртов — только routed; новые — отдельный proposal
- Не трогать circuit breaker thresholds без real prod data
