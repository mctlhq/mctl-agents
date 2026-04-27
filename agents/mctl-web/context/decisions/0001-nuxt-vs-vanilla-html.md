# 0001. Nuxt 4 SSG вместо vanilla HTML

**Status:** accepted
**Date:** 2026-04-01

## Context
Изначально mctl-web был набором статических HTML/CSS/JS файлов в `static/`,
раздаваемых nginx'ом (см. устаревший CLAUDE.md в репо). С ростом количества
страниц (`/`, `/docs`, `/privacy`, формы тенант-онбординга, multi-step форма
регистрации с GitHub OAuth) поддерживать копипаст-навигацию и общие компоненты
вручную стало непрактично.

## Decision
Переехали на **Nuxt 4** (Vue 3) с SSR=true и prerender для landing-страниц
(`/`, `/privacy`, `/docs`). Cloudflare Worker остался для динамики (`/api/*`).

Стек:
- Nuxt 4.3.1 + Vue 3.5.30
- vee-validate 4.15.1 + yup 1.7.1 для валидации формы
- @vueuse/core 14.2.1 для composables
- sass для стилей

## Consequences
- **+** Типобезопасность через TypeScript, single-file components, hot reload в dev
- **+** Prerender = SEO-friendly статика без runtime-цены
- **+** Переиспользуемые компоненты (формы, навигация, блоки фич)
- **−** Build step (раньше можно было `git push` → nginx сразу видит)
- **−** Размер бандла больше чем у vanilla HTML
- **−** Зависимость от Nuxt экосистемы (надо следить за релизами и breaking changes)

## Что НЕ предлагать (для analyst/researcher)
- Возврат к vanilla HTML / другому фреймворку без сильного обоснования
- Замену vee-validate+yup на альтернативы (zod, custom validation) — стоит того только если найдётся конкретный bug или perf-проблема
- Удаление Cloudflare Worker — он необходим для OAuth callback и rate-limiting
