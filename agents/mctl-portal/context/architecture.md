# Architecture: mctl-portal

## Purpose
Internal developer portal on Backstage (`https://app.mctl.ai`). Service catalog, scaffolder for onboarding a new service into a tenant, k8s/observability viewers, TechDocs.

## Tech stack
- **Backstage** (latest, `backstage-cli`-based)
- **Node.js 22 || 24** (engines.node)
- **TypeScript**
- **yarn workspaces**: `packages/*` (app, backend) + `plugins/*` (custom plugins)
- **playwright** for e2e
- **prettier** + Backstage lint
- Serving: nginx + Docker → mctl-gitops → ArgoCD (tenant `admins`)

## Backstage plugins (in use)
- **catalog** + **catalog-import** — component registry
- **scaffolder** — onboarding forms (tied to mctl-gitops via Argo Workflow)
- **kubernetes** — pods/services/CRDs viewer
- **techdocs** — markdown docs alongside the service
- **search** — full-text
- **observability** (custom plugin) — graphs from Prometheus
- **kubernetes-permissions** — who sees what
- **proxy** — for external APIs
- **github-actions** / **github** — CI status

## Auth
- **Dex JWT** via ops.mctl.me/api/dex — single SSO
- Sessions in the backend are stored in Postgres
- Permission framework — RBAC via group mapping

## External integrations
- **mctl-api** — for read operations (tenants, statuses)
- **Vault** via ExternalSecret — secrets
- **mctl-gitops** — the scaffolder commits to this repo
- **Argo Workflows** — tenant/service provisioning via workflow templates
- **Prometheus / Loki / Grafana** — observability plugin
- **GitHub** (PR/issue widgets)

## Known footguns (from memory)
- **k8s-reader stale token** — on rotation the SA UID changes, kubernetes/observability plugins get 401. Fix: write a fresh token to Vault → restart pod (see `reference_backstage_k8s_reader_stale_token.md`)
- **Namespaced Components** — `metadata.namespace` in catalog-info.yaml breaks scaffolder workflows if refs are not qualified (see `feedback_backstage_namespaced_components.md`)

## Dependencies for researcher
- `backstage/backstage` — main monorepo, releases every 2 weeks
- `backstage/community-plugins` — kubernetes / techdocs / scaffolder
- Node.js LTS releases (current 22)
- TypeScript releases
- `microsoft/playwright`
- `prettier/prettier`
- `yarnpkg/yarn`
- CVEs against Backstage and React/Material UI (via core plugins)
- The specific Backstage plugins we use — monitor separately

## What NOT to do (for analyst)
- Do not propose migrating from Backstage to Port/Cortex/etc. — too expensive
- Do not propose a Backstage major upgrade on patch-day of release — wait ~a week for community-plugins compat
- Do not propose removing the observability custom plugin — it is critical
