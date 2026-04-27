# 0002. S3-backed state + canary + restore-state probe

**Status:** accepted
**Date:** 2026-03-15

## Context
openclaw хранит чувствительный канальный state (auth tokens для WhatsApp Web, Telegram session, iMessage cookies, OAuth refresh-токены и т.п.). При рестарте pod'а без восстановления этого state каналы теряют коннект, что для `ovk` означает downtime реального клиента.

Раньше state жил только в memory + локальном volume. Несколько раз теряли auth при rollout.

## Decision
Three layers защиты:

1. **S3 как источник истины**. openclaw периодически синкает auth/sessions в S3 bucket (per tenant). При старте pod'а — pull из S3 перед открытием каналов.

2. **s3-sync canary workflow** (Argo CronWorkflow). Раз в N минут проверяет: pod реально пишет в S3 (свежий timestamp есть). Если canary fail > N циклов — alert. Перед rollout canary останавливается, после — restart с задержкой (иначе шумит ложными alerts).

3. **restore-state readiness probe**. Pod не маркируется ready, пока не подтвердит что auth/sessions восстановлены из S3. ArgoCD ждёт ready-status перед маркировкой rollout успешным.

## Consequences
- **+** Cross-pod restart resilience
- **+** Rollout безопаснее (probe ловит сломанный restore до того как трафик пойдёт)
- **+** Canary даёт раннее предупреждение о sync-проблемах
- **−** Зависимость от S3 (bucket down = pod не стартует) — митигация: backup region
- **−** Canary пропускает циклы во время rollout — учитывать в alert thresholds
- **−** Probe timeout выставлять больше чем самый медленный канал восстанавливается

## Recurring footguns (из памяти)
- Rollout без остановки canary → ложные alerts
- Слишком короткий probe timeout → ArgoCD тaймаутит pod даже если он восстанавливается успешно
- Изменение S3 bucket policy без проверки на всех тенантах
- Очистка bucket'а "для теста" — теряется auth у живых клиентов

## Что НЕ предлагать (для analyst/researcher)
- Замену S3 на что-то stateless (etcd, Redis) без серьёзного сравнения
- Отключение canary "потому что шумит" — нужно чинить причину шума
- Уменьшение probe timeout без стресс-теста на самом медленном канале
