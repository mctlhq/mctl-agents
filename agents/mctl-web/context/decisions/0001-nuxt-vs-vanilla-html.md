# 0001. Nuxt 4 SSG instead of vanilla HTML

**Status:** accepted
**Date:** 2026-04-01

## Context
Initially mctl-web was a set of static HTML/CSS/JS files in `static/`,
served by nginx (see the outdated CLAUDE.md in the repo). With the growth in the number of
pages (`/`, `/docs`, `/privacy`, tenant onboarding forms, multi-step
registration form with GitHub OAuth) maintaining copy-paste navigation and shared components
by hand became impractical.

## Decision
Moved to **Nuxt 4** (Vue 3) with SSR=true and prerender for landing pages
(`/`, `/privacy`, `/docs`). The Cloudflare Worker remains for dynamics (`/api/*`).

Stack:
- Nuxt 4.3.1 + Vue 3.5.30
- vee-validate 4.15.1 + yup 1.7.1 for form validation
- @vueuse/core 14.2.1 for composables
- sass for styles

## Consequences
- **+** Type safety via TypeScript, single-file components, hot reload in dev
- **+** Prerender = SEO-friendly static without runtime cost
- **+** Reusable components (forms, navigation, feature blocks)
- **−** Build step (previously `git push` → nginx saw it immediately)
- **−** Bundle size larger than vanilla HTML
- **−** Dependency on the Nuxt ecosystem (must track releases and breaking changes)

## What NOT to propose (for analyst/researcher)
- Reverting to vanilla HTML / another framework without strong rationale
- Replacing vee-validate+yup with alternatives (zod, custom validation) — only worth it if a specific bug or perf issue is found
- Removing the Cloudflare Worker — it is necessary for OAuth callback and rate limiting
