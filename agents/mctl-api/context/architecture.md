# Architecture: mctl-api

## Назначение
Центральный платформенный API: REST endpoints для UI/CLI/agents + MCP-сервер (Streamable HTTP) для Claude/AI-клиентов. Точка входа `https://api.mctl.ai`. Обслуживает тенант `admins`, обращается к Kubernetes API и Vault для оркестрации.

## Технологический стек
- **Go 1.24**, модули
- **chi/v5 5.2.1** — HTTP router
- **httprate 0.15** — rate limiting middleware
- **mark3labs/mcp-go 0.31** — MCP-сервер (24 тулза, Streamable HTTP transport)
- **pgx/v5 5.8** — Postgres driver (идентичности, аудит-логи)
- **client-go 0.32** — Kubernetes API клиент
- **go-oidc/v3** — OIDC проверка JWT (Dex / GitHub OAuth)
- **prometheus/client_golang 1.23** — метрики /metrics
- **uuid 1.6**, **yaml.v3** для парсинга манифестов

## Auth flow (3 типа bearer'ов)
- **GitHub PAT** (без точек) → mctl-api → GitHub API → org membership → группы тенантов из gitops
- **Dex JWT** (iss != self) → JWKS на ops.mctl.me/api/dex/keys → группы из claims
- **OAuth JWT** (iss == self) → HMAC-SHA256 verification → GitHub API для групп
- `AUTH_REQUIRED=false` локально

## MCP server
- Endpoint: `https://api.mctl.ai/mcp` (Streamable HTTP, POST + GET)
- 24 тулзы (11 read + 13 write — `get_service_status`, `get_tenant_metrics`, `list_incidents`, `trigger_workflow`, `get_workflow_logs`, identity tools, etc.)
- OAuth 2.0 PKCE для Claude.ai connector
- Auth header per-request

## Внешние интеграции
- **Vault** (`secrets.mctl.ai`) — секреты сервисов через auth/kubernetes
- **ArgoCD** (`ARGOCD_TOKEN` из Vault) — статус приложений
- **Backstage API** (`BACKSTAGE_TOKEN` из Vault) — каталог сервисов, scaffolder triggers
- **Argo Workflows** (`workflows.mctl.ai`) — submit/inspect runs
- **Postgres** — тенант-данные, audit logs
- **Kubernetes API** — pods/services/cronjobs/workflows status

## Dependencies для researcher
Researcher следит за:
- `golang/go` — major LTS bumps (мы на 1.24)
- `go-chi/chi` — security/perf в HTTP роутере
- `mark3labs/mcp-go` — критичная библиотека для MCP сервера
- `jackc/pgx` — driver для Postgres
- `kubernetes/client-go` — мажорные релизы (текущий 0.32 ↔ k8s 1.32)
- `coreos/go-oidc` — JWT validation
- `prometheus/client_golang`
- `argoproj/argo-cd` — для совместимости статусов
- `argoproj/argo-workflows` — изменения CRD/API
- CVE по всем выше через GHSA + WebSearch

## Известные ограничения
- Все write-тулзы MCP должны checkать org membership заявителя
- `pgx` connection pool tuning зависит от прод-нагрузки
- mctl-api сам авторизует tenant scope — ошибка тут = cross-tenant утечка

## Что НЕ делать (для analyst)
- Не предлагать смену Go-роутера на gin/echo без сильного benchmark'а
- Не предлагать заменять pgx на ORM (gorm) — потеряем control над запросами
- Не предлагать переход на gRPC для MCP — клиенты Claude.ai требуют HTTP
