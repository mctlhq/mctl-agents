# Architecture: mctl-gitops

## Назначение
GitOps source-of-truth для всей платформы. ArgoCD watch'ит этот репо и реконсилит cluster state. Содержит: per-tenant Helm charts, Argo Workflow templates, Backstage scaffolder templates, Terraform для кластерной инфры, Go CLI tool для платформенных операций.

## Структура (key paths)
- `platform-gitops/apps/` — ArgoCD Application definitions (App-of-Apps pattern)
- `platform-gitops/services/<tenant>/<svc>/` — per-tenant service конфиги
- `platform-gitops/argo-workflows/cluster-templates/` — CronWorkflow + ClusterWorkflowTemplate
- `platform-gitops/argo-workflows/secrets/` — ExternalSecret манифесты
- `platform-gitops/argo-workflows/service-templates/` — Helm value templates для scaffolder
- `platform-gitops/argo-workflows/file-templates/` — другие YAML templates
- `platform-gitops/backstage-templates/` — Backstage scaffolder skeleton templates
- `platform-gitops/helm-charts/base-service/` — generic Helm chart для большинства сервисов
- `platform-gitops/bootstrap/` — bootstrap App-of-Apps (что разворачивает что)
- `platform-gitops/agents-state/` — runtime state mctl-agents (этот репо!) — proposals, inbox, digest
- `infrastructure/` — Terraform, cluster bootstrap
- `cli/mctl/` — Go CLI tool (отдельный go.mod, **Go 1.25**)

## Технологический стек
- **Kubernetes manifests** (raw YAML)
- **Helm** charts (base-service, openclaw, custom)
- **Argo Workflows** + **Argo Rollouts**
- **ArgoCD ApplicationSet** для генерации Apps по directory pattern
- **External Secrets Operator** + **Vault** ClusterSecretStore (`vault-backend`)
- **Terraform** (под `infrastructure/`)
- **Go 1.25** для `cli/mctl/`

## ArgoCD App-of-Apps
Bootstrap chart разворачивает несколько ApplicationSets:
- `apps` — ApplicationSet, генерирует Apps по pattern `services/*/*`
- `tenants` — ApplicationSet, генерирует Apps для тенант-namespaces
- `openclaw-skills` — ApplicationSet для openclaw skill overlays

Внутри каждой App: `helm-charts/base-service` + `services/<tenant>/<svc>/values.yaml`.

## Conventions
- YAML: 2-space indent, `{{- ... }}` для whitespace control в Helm
- Никаких hardcoded secrets — через Vault + ExternalSecrets
- Каждое изменение платформы = git commit здесь
- Каждое значимое решение = ADR

## Внешние интеграции
- **GitHub** — push (через deploy key `mctl-gitops-deploy-key`) и через GitHub App credentials для bot-операций
- **Vault** (`secrets.mctl.ai`) — все секреты тенантов и платформы
- **ArgoCD** (`ops.mctl.ai`) — sync engine
- **Argo Workflows** — все cron / on-demand pipelines

## Dependencies для researcher
- `argoproj/argo-cd` — мажорные релизы могут менять Application API / sync behavior
- `argoproj/argo-workflows` — CRD changes, executor improvements
- `argoproj/argo-rollouts`
- `helm/helm` — major releases
- `external-secrets/external-secrets` — operator updates
- `cert-manager/cert-manager` — TLS lifecycle
- `prometheus/prometheus` + `grafana/loki` (если templated)
- `hashicorp/vault` — server-side
- `hashicorp/terraform` provider versions
- CVE по перечисленным выше

## Известные особенности
- **Argo v3.7.10 archive INSERT NULL bug** (см. `reference_argo_archive_bug.md`) — TTL goroutine ок, archive worker wedges каждый Succeeded workflow на missing-NOT-NULL constraint. Bump до v3.7.12+/v3.8.x или hotfix schema.
- **Builds & deploys централизованы здесь** (см. `feedback_builds_in_gitops.md`) — индивидуальные репо имеют только PR validation CI; реальные image build и rollout — через workflows в этом репо. Исключение: mctl-web (CF Worker через wrangler в своём репо).
- **agents-state/** — пишет mctl-agents автоматически. Не редактируй руками.

## Что НЕ делать (для analyst)
- Не предлагать миграцию с ArgoCD на Flux — слишком дорого, broad blast radius
- Не предлагать апгрейд Argo major на patch-day — комбинированно ломалось ранее (Argo 3.7.10 bug — пример)
- Не предлагать добавление manual approval gates в обход ApplicationSet — это эскалация процесса, требует социального buy-in
- Не предлагать удалять `agents-state/` — это runtime data mctl-agents
