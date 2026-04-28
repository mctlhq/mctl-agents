# Architecture: mctl-web

## Purpose
Public site `mctl.ai` (landing + docs + privacy) and Cloudflare Worker for OAuth, tenant creation request forms, and Telegram notifications.

## Tech stack

### Frontend (Nuxt SSG)
- **Nuxt 4.3.1** (SSR=true, prerender for `/`, `/privacy`, `/docs`)
- **Vue 3.5.30** + vue-router 4.6.4
- **vee-validate 4.15.1** + **yup 1.7.1** — tenant form validation
- **@vueuse/core 14.2.1** — composables
- **sass 1.98.0** + **vite-svg-loader 5.1.1**
- Font: JetBrains Mono (Google Fonts preconnect)
- Build: `nuxt build` → `dist/` → served as static

### Cloudflare Worker (`cloudflare-worker/`)
- Routes: `mctl.ai/api/*`, plus redirects from `mctl.me/*`, `*.mctl.me/*`, `mctl.ru/*`, `*.mctl.ru/*` to `mctl.ai`
- Endpoints: `/api/github/login`, `/api/github/callback`, `/api/submit` (tenant provisioning via Backstage), `/api/contact`
- Rate limits: 5/5min on /submit, 3/5min on /contact, 10/min on /github/login
- Deploy: Wrangler via GitHub Actions (`deploy.yml` in this repo) — **exception from centralized builds in mctl-gitops**

### Serving
- Cloudflare Pages / nginx static for the Nuxt build
- Worker — separate deploy via wrangler

## Routes (Nuxt pages)
- `/` — landing (`app/pages/index.vue`)
- `/docs` — platform overview (`app/pages/docs/index.vue`)
- `/privacy` — privacy policy (`app/pages/privacy/index.vue`)

> Memory mentioned `/mcp` as the connector page — at the time of the current commit it is **not** in `app/pages/`. Possibly moved to docs.mctl.ai (separate `mctl-docs` repo).

## External integrations
- **GitHub OAuth App** (`GITHUB_CLIENT_ID` / `SECRET` / `OAUTH_HMAC_KEY`) — user login via the GitHub org `mctlhq`
- **Backstage API** (`app.mctl.ai`, `BACKSTAGE_LANDING_TOKEN`) — team name availability check + provisioning workflow trigger for a new tenant
- **Telegram Bot** (`TELEGRAM_BOT_TOKEN` / `CHAT_ID`) — notifications on every new request
- **Resend** (`RESEND_API_KEY`) — welcome email after successful registration

## Worker secrets (Cloudflare Dashboard / wrangler secret)
- `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- `GITHUB_CLIENT_ID`, `GITHUB_CLIENT_SECRET`, `GITHUB_OAUTH_HMAC_KEY`
- `BACKSTAGE_LANDING_TOKEN` (HMAC-SHA256, must match the env of the Backstage pod)
- `RESEND_API_KEY`

## Dependencies for researcher (release tracking)
The researcher monitors these repos via `WebFetch https://github.com/<owner>/<repo>/releases/latest`:

- `nuxt/nuxt` — main framework
- `vuejs/core` — Vue 3
- `vuejs/router` — vue-router
- `logaretm/vee-validate` — forms
- `jquense/yup` — validation schemas
- `vueuse/vueuse` — composables
- `sass/dart-sass` — styles
- `cloudflare/workers-sdk` — wrangler / Worker runtime
- `cloudflare/workerd` — Worker runtime engine

## Known limitations / nuances
- **mctlhq.CLAUDE.md** in the repo is outdated — it describes "static HTML/CSS/JS, no frameworks", but in reality it is now Nuxt 4. Do not trust CLAUDE.md as a source of truth — look at `package.json` + `nuxt.config.ts`.
- The build pipeline (`deploy.yml`) lives **in this repo**, not in mctl-gitops — the only service with such an exception.
- `runtimeConfig.apiSecret = '123'` in `nuxt.config.ts` — placeholder, do not use for prod logic.
