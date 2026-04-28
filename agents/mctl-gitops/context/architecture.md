# Architecture: mctl-gitops

## Purpose
GitOps source of truth for the entire platform. ArgoCD watches this repo and reconciles cluster state. Contains: per-tenant Helm charts, Argo Workflow templates, Backstage scaffolder templates, Terraform for cluster infra, Go CLI tool for platform operations.

## Structure (key paths)
- `platform-gitops/apps/` — ArgoCD Application definitions (App-of-Apps pattern)
- `platform-gitops/services/<tenant>/<svc>/` — per-tenant service configs
- `platform-gitops/argo-workflows/cluster-templates/` — CronWorkflow + ClusterWorkflowTemplate
- `platform-gitops/argo-workflows/secrets/` — ExternalSecret manifests
- `platform-gitops/argo-workflows/service-templates/` — Helm value templates for the scaffolder
- `platform-gitops/argo-workflows/file-templates/` — other YAML templates
- `platform-gitops/backstage-templates/` — Backstage scaffolder skeleton templates
- `platform-gitops/helm-charts/base-service/` — generic Helm chart for most services
- `platform-gitops/bootstrap/` — bootstrap App-of-Apps (what deploys what)
- `platform-gitops/agents-state/` — runtime state of mctl-agents (this repo!) — proposals, inbox, digest
- `infrastructure/` — Terraform, cluster bootstrap
- `cli/mctl/` — Go CLI tool (separate go.mod, **Go 1.25**)

## Tech stack
- **Kubernetes manifests** (raw YAML)
- **Helm** charts (base-service, openclaw, custom)
- **Argo Workflows** + **Argo Rollouts**
- **ArgoCD ApplicationSet** for generating Apps via directory pattern
- **External Secrets Operator** + **Vault** ClusterSecretStore (`vault-backend`)
- **Terraform** (under `infrastructure/`)
- **Go 1.25** for `cli/mctl/`

## ArgoCD App-of-Apps
The bootstrap chart deploys several ApplicationSets:
- `apps` — ApplicationSet, generates Apps via the pattern `services/*/*`
- `tenants` — ApplicationSet, generates Apps for tenant namespaces
- `openclaw-skills` — ApplicationSet for openclaw skill overlays

Inside each App: `helm-charts/base-service` + `services/<tenant>/<svc>/values.yaml`.

## Conventions
- YAML: 2-space indent, `{{- ... }}` for whitespace control in Helm
- No hardcoded secrets — through Vault + ExternalSecrets
- Every platform change = a git commit here
- Every significant decision = an ADR

## External integrations
- **GitHub** — push (via deploy key `mctl-gitops-deploy-key`) and via GitHub App credentials for bot operations
- **Vault** (`secrets.mctl.ai`) — all tenant and platform secrets
- **ArgoCD** (`ops.mctl.ai`) — sync engine
- **Argo Workflows** — all cron / on-demand pipelines

## Dependencies for researcher
- `argoproj/argo-cd` — major releases may change Application API / sync behavior
- `argoproj/argo-workflows` — CRD changes, executor improvements
- `argoproj/argo-rollouts`
- `helm/helm` — major releases
- `external-secrets/external-secrets` — operator updates
- `cert-manager/cert-manager` — TLS lifecycle
- `prometheus/prometheus` + `grafana/loki` (if templated)
- `hashicorp/vault` — server-side
- `hashicorp/terraform` provider versions
- CVEs across the above

## Known specifics
- **Argo v3.7.10 archive INSERT NULL bug** (see `reference_argo_archive_bug.md`) — TTL goroutine ok, archive worker wedges every Succeeded workflow on a missing-NOT-NULL constraint. Bump to v3.7.12+/v3.8.x or hotfix the schema.
- **Builds & deploys are centralized here** (see `feedback_builds_in_gitops.md`) — individual repos have only PR validation CI; actual image builds and rollouts go through workflows in this repo. Exception: mctl-web (CF Worker via wrangler in its own repo).
- **agents-state/** — written by mctl-agents automatically. Do not edit by hand.

## What NOT to do (for analyst)
- Do not propose migrating from ArgoCD to Flux — too expensive, broad blast radius
- Do not propose an Argo major upgrade on patch-day — has broken in combination before (Argo 3.7.10 bug — example)
- Do not propose adding manual approval gates around ApplicationSet — this is a process escalation, requires social buy-in
- Do not propose deleting `agents-state/` — that is runtime data of mctl-agents
