# 0001. VitePress 1.6 для документационного портала

**Status:** accepted
**Date:** 2026-03-28

## Context
До 2026-03 у платформы mctl не было выделенного документационного портала. Документация мигрировала между разными местами (README в репо mctl-web, страница `/docs` на mctl.ai). С ростом количества сервисов (mctl-api, mctl-portal, mctl-agent, mctl-openclaw, etc.) такая раздробленность стала мешать onboarding'у новых тенантов и партнёров.

## Decision
Создан отдельный репо `mctl-docs` на **VitePress 1.6** с раздачей через `docs.mctl.ai`. Стек:
- VitePress 1.6 (Vue 3 под капотом, SSG output)
- mermaid 11 для диаграмм
- TypeScript для конфигов
- nginx + Docker → mctl-gitops → ArgoCD деплой

## Consequences
- **+** Один URL для всей пользовательской документации
- **+** SSG = быстрая раздача, SEO-friendly
- **+** Markdown + Vue компоненты при необходимости
- **+** Легко контрибьютить (PR в .md файл)
- **−** Build step (~30 сек на полный rebuild)
- **−** Нет встроенного versioning — старые версии доков теряются при апгрейде
- **−** Mermaid добавляет ~200KB к bundle

## Что НЕ предлагать (для analyst/spec-writer mctl-docs агента)
- Замену VitePress на Docusaurus / MkDocs / GitBook без сильного обоснования (миграция дорогая, ROI неочевиден).
- Введение i18n до того как у платформы появится non-English аудитория.
- Сложные кастомные Vue-компоненты вместо стандартного markdown — повышает порог contribution'а.
