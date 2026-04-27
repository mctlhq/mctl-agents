# 0001. Backstage как developer portal

**Status:** accepted
**Date:** 2026-01-15

## Context
Платформе нужен self-service portal для команд: онбординг сервиса, k8s status viewer, observability dashboards в одном месте, scaffolder для генерации шаблонов. Альтернативы: Port, Cortex, Compass, custom Vue/React app.

## Decision
**Backstage** (open-source, Spotify origin) с custom плагинами для платформенной специфики (observability, openclaw integration, mctl-api proxy).

## Consequences
- **+** Богатая экосистема плагинов (kubernetes, techdocs, scaffolder, github, search)
- **+** Active community, регулярные релизы каждые 2 недели
- **+** Open-source, нет vendor lock-in
- **+** Yarn workspaces — легко добавлять custom плагины
- **−** Вес monorepo (yarn build/install заметные)
- **−** Backstage major bumps требуют ре-валидации каждого плагина
- **−** Permissions framework — крутая кривая обучения

## Что НЕ предлагать (для analyst/researcher)
- Миграцию с Backstage на SaaS-альтернативу (Port/Cortex/Compass) — потеря данных + плагинов
- Полный rewrite на custom React app — Backstage решает 80% бесплатно
- Bumping Backstage major сразу при выходе — community-plugins compat обычно отстаёт на 1-2 недели
