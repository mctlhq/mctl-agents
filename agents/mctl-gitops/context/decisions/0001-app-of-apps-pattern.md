# 0001. ArgoCD App-of-Apps + ApplicationSet pattern

**Status:** accepted
**Date:** 2026-01-10

## Context
Платформе нужно разворачивать N сервисов в M тенант-неймспейсах (admins, labs, ovk, …) с минимальной болью при добавлении новых тенантов или сервисов. Прямое создание ArgoCD Application для каждой комбинации = N*M ручной работы.

## Decision
**App-of-Apps** через bootstrap chart, плюс **ApplicationSet** для генерации:
- `apps` ApplicationSet с git directory generator pattern `services/*/*` — автоматически создаёт ArgoCD Application для каждого `services/<tenant>/<svc>/`
- `tenants` ApplicationSet — namespace + RBAC + quotas per tenant
- `openclaw-skills` ApplicationSet — overlay skills per openclaw тенант

## Consequences
- **+** Добавление сервиса = git commit `services/<tenant>/<svc>/values.yaml`, App автоматически создаётся
- **+** Добавление тенанта = `services/<new-tenant>/`, namespace+sync auto
- **+** Полный audit trail в git
- **−** Изменение pattern (например: смена базового chart'а) затрагивает все сервисы сразу
- **−** Сложность ApplicationSet templates — учиться нужно

## Что НЕ предлагать
- Миграцию с ArgoCD на Flux (потеря всех ApplicationSets)
- Прямые Application манифесты в обход ApplicationSet — теряем self-service
- Helm-of-helms wrapper — ApplicationSet это уже решает на уровне ArgoCD
