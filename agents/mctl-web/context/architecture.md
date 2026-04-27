# Architecture: mctl-web

## Назначение
Публичный сайт `mctl.ai` (landing + docs + privacy) и Cloudflare Worker для OAuth, форм заявок на создание тенанта и Telegram-уведомлений.

## Технологический стек

### Frontend (Nuxt SSG)
- **Nuxt 4.3.1** (SSR=true, prerender для `/`, `/privacy`, `/docs`)
- **Vue 3.5.30** + vue-router 4.6.4
- **vee-validate 4.15.1** + **yup 1.7.1** — валидация формы тенанта
- **@vueuse/core 14.2.1** — composables
- **sass 1.98.0** + **vite-svg-loader 5.1.1**
- Шрифт: JetBrains Mono (Google Fonts preconnect)
- Билд: `nuxt build` → `dist/` → раздаётся статикой

### Cloudflare Worker (`cloudflare-worker/`)
- Routes: `mctl.ai/api/*`, плюс редиректы с `mctl.me/*`, `*.mctl.me/*`, `mctl.ru/*`, `*.mctl.ru/*` на `mctl.ai`
- Endpoints: `/api/github/login`, `/api/github/callback`, `/api/submit` (tenant provisioning через Backstage), `/api/contact`
- Rate limits: 5/5min на /submit, 3/5min на /contact, 10/min на /github/login
- Деплой: Wrangler через GitHub Actions (`deploy.yml` в этом репо) — **исключение из централизованных билдов в mctl-gitops**

### Раздача
- Cloudflare Pages / nginx статика для Nuxt-билда
- Worker — отдельный деплой через wrangler

## Маршруты (Nuxt pages)
- `/` — landing (`app/pages/index.vue`)
- `/docs` — обзор платформы (`app/pages/docs/index.vue`)
- `/privacy` — privacy policy (`app/pages/privacy/index.vue`)

> Память упоминала `/mcp` как connector page — на момент текущего commit'а её **нет** в `app/pages/`. Возможно перенесена в docs.mctl.ai (отдельный репо `mctl-docs`).

## Внешние интеграции
- **GitHub OAuth App** (`GITHUB_CLIENT_ID` / `SECRET` / `OAUTH_HMAC_KEY`) — логин пользователей через GitHub org `mctlhq`
- **Backstage API** (`app.mctl.ai`, `BACKSTAGE_LANDING_TOKEN`) — проверка доступности team name + запуск provisioning workflow для нового тенанта
- **Telegram Bot** (`TELEGRAM_BOT_TOKEN` / `CHAT_ID`) — уведомления на каждую новую заявку
- **Resend** (`RESEND_API_KEY`) — welcome email после успешной регистрации

## Worker secrets (Cloudflare Dashboard / wrangler secret)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_OAUTH_HMAC_KEY`
- `BACKSTAGE_LANDING_TOKEN` (HMAC-SHA256, должен совпадать с env Backstage пода)
- `RESEND_API_KEY`

## Dependencies для researcher (трекинг релизов)
Researcher следит за этими репо через `WebFetch https://github.com/<owner>/<repo>/releases/latest`:

- `nuxt/nuxt` — основной фреймворк
- `vuejs/core` — Vue 3
- `vuejs/router` — vue-router
- `logaretm/vee-validate` — формы
- `jquense/yup` — схемы валидации
- `vueuse/vueuse` — composables
- `sass/dart-sass` — стили
- `cloudflare/workers-sdk` — wrangler / Worker runtime
- `cloudflare/workerd` — Worker runtime engine

## Известные ограничения / нюансы
- **mctlhq.CLAUDE.md** в репо устарел — там описан "static HTML/CSS/JS, no frameworks", но реально сейчас Nuxt 4. Не доверять CLAUDE.md как источнику истины — смотреть `package.json` + `nuxt.config.ts`.
- Build pipeline (`deploy.yml`) живёт **в этом репо**, а не в mctl-gitops — единственный сервис с таким исключением.
- `runtimeConfig.apiSecret = '123'` в `nuxt.config.ts` — placeholder, не использовать для prod-логики.
