# 0001. ArgoCD App-of-Apps + ApplicationSet pattern

**Status:** accepted
**Date:** 2026-01-10

## Context
The platform needs to deploy N services across M tenant namespaces (admins, labs, ovk, …) with minimal pain when adding new tenants or services. Direct creation of an ArgoCD Application for each combination = N*M of manual work.

## Decision
**App-of-Apps** via a bootstrap chart, plus **ApplicationSet** for generation:
- `apps` ApplicationSet with git directory generator pattern `services/*/*` — automatically creates an ArgoCD Application for each `services/<tenant>/<svc>/`
- `tenants` ApplicationSet — namespace + RBAC + quotas per tenant
- `openclaw-skills` ApplicationSet — overlay skills per openclaw tenant

## Consequences
- **+** Adding a service = git commit `services/<tenant>/<svc>/values.yaml`, App is created automatically
- **+** Adding a tenant = `services/<new-tenant>/`, namespace+sync auto
- **+** Full audit trail in git
- **−** Changing the pattern (e.g. switching the base chart) affects all services at once
- **−** ApplicationSet template complexity — there is a learning curve

## What NOT to propose
- Migrating from ArgoCD to Flux (loss of all ApplicationSets)
- Direct Application manifests around ApplicationSet — we lose self-service
- Helm-of-helms wrapper — ApplicationSet already solves this at the ArgoCD level
