# Architecture: mctl-portal

## Назначение
Внутренний developer portal на Backstage (`https://app.mctl.ai`). Каталог сервисов, scaffolder для онбординга нового сервиса в тенант, k8s/observability viewers, TechDocs.

## Технологический стек
- **Backstage** (latest, `backstage-cli`-based)
- **Node.js 22 || 24** (engines.node)
- **TypeScript**
- **yarn workspaces**: `packages/*` (app, backend) + `plugins/*` (custom plugins)
- **playwright** для e2e
- **prettier** + Backstage lint
- Раздача: nginx + Docker → mctl-gitops → ArgoCD (тенант `admins`)

## Backstage плагины (используем)
- **catalog** + **catalog-import** — реестр компонентов
- **scaffolder** — onboarding-формы (со связкой mctl-gitops через Argo Workflow)
- **kubernetes** — pods/services/CRDs viewer
- **techdocs** — markdown-доки рядом с сервисом
- **search** — фуллтекст
- **observability** (custom plugin) — графики из Prometheus
- **kubernetes-permissions** — кто что видит
- **proxy** — для внешних API
- **github-actions** / **github** — статус CI

## Auth
- **Dex JWT** через ops.mctl.me/api/dex — единый SSO
- Sessions в backend хранятся в Postgres
- Permission framework — RBAC через group-mapping

## Внешние интеграции
- **mctl-api** — для read-операций (тенанты, статусы)
- **Vault** через ExternalSecret — secrets
- **mctl-gitops** — scaffolder коммитит в этот репо
- **Argo Workflows** — провижионинг тенанта/сервиса через workflow templates
- **Prometheus / Loki / Grafana** — observability plugin
- **GitHub** (PR/issue widgets)

## Известные footguns (из памяти)
- **k8s-reader stale token** — при rotation SA UID меняется, kubernetes/observability plugins получают 401. Fix: записать свежий token в Vault → restart pod (см. `reference_backstage_k8s_reader_stale_token.md`)
- **Namespaced Components** — `metadata.namespace` в catalog-info.yaml ломает scaffolder workflows если не qualify ref'ы (см. `feedback_backstage_namespaced_components.md`)

## Dependencies для researcher
- `backstage/backstage` — main monorepo, релизы каждые 2 недели
- `backstage/community-plugins` — kubernetes / techdocs / scaffolder
- Node.js LTS releases (текущий 22)
- TypeScript releases
- `microsoft/playwright`
- `prettier/prettier`
- `yarnpkg/yarn`
- CVE по Backstage и React/Material UI (через core плагины)
- Specific Backstage плагины которые мы используем — мониторить отдельно

## Что НЕ делать (для analyst)
- Не предлагать миграцию с Backstage на Port/Cortex/etc. — слишком дорого
- Не предлагать апгрейд Backstage major на patch-day выпуска — ждать ~неделю на comm-plugins compat
- Не предлагать удаление observability custom плагина — он критичен
