# 0001. Три отдельных тенанта вместо single multi-user деплоя

**Status:** accepted
**Date:** 2026-02-01

## Context
openclaw поддерживает multi-user gateway, но его state (auth tokens, channel sessions, S3-bucket scoping) сложно безопасно изолировать в одном инстансе. Команда mctl эксплуатирует openclaw для:
- внутренних нужд (`admins`)
- эксперементов с beta-features (`labs`)
- production клиента с высоким SLA (`ovk`)

Слияние в single deployment означало бы blast radius всех изменений на production клиента.

## Decision
Развёрнуты **три независимых deployment'а** openclaw в отдельных Kubernetes namespaces (`admins`, `labs`, `ovk`). Каждый имеет:
- свой S3 bucket для state
- свой helm release в mctl-gitops
- свой rollout pipeline
- shared **3-layer skills** (built-in + YAML + remote) для уменьшения дублирования

Изменения катятся в порядке: `labs` → (наблюдение N дней) → `admins` → `ovk`.

## Consequences
- **+** Полная изоляция state и blast radius
- **+** `labs` служит canary для `ovk`
- **+** `ovk` SLA не зависит от экспериментов команды
- **−** 3x ресурсов
- **−** 3x операционных задач (rollouts, мониторинг, тех-долг)
- **−** Skills в trio синхронизировать руками (если общие)

## Что НЕ предлагать (для analyst/researcher)
- Слияние тенантов в один деплой — это явно отвергнуто
- Удаление `labs` для экономии ресурсов — он критичен как canary для `ovk`
- Прямой rollout в `ovk` без прохождения через `labs`
