# Architecture: mctl-docs

## Назначение
Публичный документационный портал `docs.mctl.ai`. Источник истины для платформенной документации mctl: getting-started, гайды, MCP, API reference, security, platform internals.

## Технологический стек
- **VitePress 1.6** (Vue-based static site generator)
- **mermaid 11** для диаграмм (включается через VitePress плагин)
- **TypeScript** (tsconfig.json) — для конфигов и кастомного theme
- Билд: `vitepress build docs` → `docs/.vitepress/dist/`
- Раздача: nginx + Dockerfile (см. `mctl-docs/Dockerfile`, `mctl-docs/nginx.conf`)
- Деплой: через mctl-gitops → ArgoCD

## Структура `docs/` (VitePress root)

| Папка | Назначение |
|---|---|
| `docs/.vitepress/` | Конфиг сайта: `config.{ts,mts}`, theme overrides, sidebar/nav |
| `docs/getting-started/` | Onboarding, "first 5 minutes" туториал, первый сервис |
| `docs/guides/` | How-to статьи (deploy сервиса, secrets, custom domain, etc.) |
| `docs/platform/` | Платформенные концепции (тенанты, ArgoCD flow, Backstage) |
| `docs/mcp/` | MCP-сервер mctl-api: список тулзов, OAuth flow, примеры |
| `docs/api/` | REST API reference от mctl-api |
| `docs/security/` | Auth модели, секреты, vault, threat model |
| `docs/reference/` | Шаблоны helm charts, configmaps, конвенции |
| `docs/public/` | Статические assets (логотипы, og-image) |

> Подробный snapshot текущей структуры (с короткими аннотациями каждой страницы) — в `context/docs-tree.md`.

## Что НЕ документируем
- Внутренние реализационные детали отдельных репо (для них есть CLAUDE.md в каждом репо)
- Команды разработки (live-reload, debug) — это в README соответствующего репо
- Code review процесс (PR convention, codex review) — в `.claude/CLAUDE.md` репо

## Внешние интеграции
- **mctl-api** — главный source-of-truth для `docs/api/` и `docs/mcp/`
- **mctl-portal (Backstage)** — для `docs/platform/backstage*.md`
- **mctl-gitops** — для `docs/guides/gitops*.md`, `docs/reference/helm*.md`

## Conventions для doc-агента
- **Frontmatter** — VitePress поддерживает YAML frontmatter (title, description, layout). Используй для overrides.
- **Cross-links** — root-relative, без расширения (например `[MCP overview](/mcp/overview)`).
- **Code blocks** — указывай язык (` ```bash`, ` ```yaml`).
- **Mermaid** — ` ```mermaid` для диаграмм flow / sequence / state.
- **Сленг тенантов** — везде `admins/labs/ovk` нижним регистром, как в платформе.
- **English only для пользовательской документации** (хотя CLAUDE.md в этом репо — на русском, как и весь mctl-агентный код).

## Известные ограничения
- VitePress 1.6 — не используется новейший 2.x; bump при обновлении должен учитывать breaking changes.
- Билд односекционный (один `docs/`), нет concept of versioned docs (старые версии теряются при апгрейде).
- Mermaid bundles add ~200KB к bundle size.

## Mctl MCP (для researcher проверки prod-версий сервисов)
Тулзы `mcp__mctl__*` могут вернуть текущие версии mctl-api / mctl-web / etc. — это критично чтобы документировать только то, что юзер реально может использовать СЕЙЧАС, а не PR в feature-ветке.
