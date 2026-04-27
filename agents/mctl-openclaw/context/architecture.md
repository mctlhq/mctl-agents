# Architecture: mctl-openclaw

## Назначение
Деплой [openclaw](https://github.com/openclaw/openclaw) (multi-channel AI gateway) на платформе mctl. Три параллельных тенант-инстанса с независимым state, общей 3-layer skills архитектурой и shared canary/probe защитой.

## Технологический стек
- **Node.js** + TypeScript (workspace packages)
- **openclaw 2026.3.14** (см. `current-version.md`) — upstream `github.com/openclaw/openclaw`, MIT
- **Plugin SDK**: extensions живут в `extensions/*`, импортируют `openclaw/plugin-sdk/*`
- **Каналы**: WhatsApp (web), Telegram, Discord, Slack, Signal, iMessage, BlueBubbles, Matrix, MS Teams, IRC, LINE, Mattermost, Nextcloud Talk, Nostr, Synology Chat, Tlon, Twitch, Zalo (+Personal), WeChat, QQ, WebChat, Feishu, Google Chat
- **Раздача**: Mintlify docs на `docs.openclaw.ai`, installers на `openclaw.ai/install*`
- **Билд / деплой**: Docker → mctl-gitops → ArgoCD

## Тенанты на mctl
Три деплоя openclaw в Kubernetes:

### `admins` (admins тенант)
- Системный деплой для команды mctlhq
- Полный набор каналов
- Самый низкий blast radius (внутренний)

### `labs` (labs тенант)
- Экспериментальный деплой (новые features, beta extensions)
- **Близок к лимиту памяти** — любое увеличение footprint требует обоснования
- Используется для прогона новых каналов перед промо в `ovk`

### `ovk` (ovk тенант)
- Production деплой для конкретного клиента
- Высокий SLA, рестарты болезненны
- Изменения только после прогона в `labs`

## Shared 3-layer skills архитектура
Skills layout повторяется в каждом тенанте:
1. **Layer 1: Built-in skills** (compiled в openclaw core)
2. **Layer 2: YAML skills** (hot-reload из `skills/custom/`)
3. **Layer 3: Remote skills** (HTTP-delegated, регистрация через REST API)

При обновлении skill в одном тенанте — обновляется в trio (если общий). Раздельные skills возможны через tenant-specific overlays в gitops.

## Защита состояния (state guards)
Из памяти команды (см. `reference_openclaw_state_persistence.md`):

### s3-sync canary
Периодический workflow проверяет что openclaw реально пишет в S3:
- Если canary пропускает > N циклов → alert
- При rollout: canary остановлен на время rollout, после — restart с задержкой

### restore-state probe
Readiness probe в pod'е проверяет что openclaw восстановил sessions/auth из S3:
- Если probe не проходит за timeout → ArgoCD не маркирует rollout успешным
- Особенно важно для `ovk` (нельзя терять auth для production клиентов)

## Где живёт state
- Auth tokens / sessions — S3 (cross-pod restart resilience)
- Канальные cookies (WhatsApp web, Telegram session) — S3 + memory
- Skill metrics — SQLite в pod (теряется при restart, не критично)
- Conversation history — каждый канал по-своему (см. `extensions/<channel>/`)

## Внешние интеграции
- **mctl-api** через MCP (`api.mctl.ai/mcp`) — статус деплоев, метрики
- **Mintlify** — docs.openclaw.ai
- **GitHub openclaw/openclaw** — upstream, fork-tracking

## Dependencies для researcher (трекинг релизов)
Researcher следит за этими источниками через `WebFetch`:

- `openclaw/openclaw` releases — главный upstream
- `openclaw/openclaw` issues — особенно `bug` + `security` лейблы
- `openclaw/nix-openclaw` — Nix packaging
- Каналы (если меняются API):
  - `whiskeysockets/Baileys` — WhatsApp Web reverse-engineered
  - `discordjs/discord.js`
  - `slackapi/node-slack-sdk`
- Node.js LTS releases — major bumps требуют валидации
- TypeScript releases — strict type checks

## Известные ограничения / footguns
- **`labs` тенант** — не пушить туда изменения, повышающие RAM. Даже на 50MB.
- **Канал-агностичные refactor'ы** — затрагивают **все** built-in + extension каналы; нужно проверить routing/allowlist/pairing/onboarding/docs (см. CLAUDE.md upstream репо)
- **`workspace:*` в plugin dependencies** — ломает `npm install --omit=dev`. Использовать `peerDependencies`/`devDependencies` для core
- **CODEOWNERS-restricted paths** — не редактировать без явного запроса owner'а
- **rollout без canary** — риск тихой потери S3-sync, обнаружится только когда pod'ы потеряют auth

## Что НЕ делать (для analyst/researcher)
- Не предлагать рестарты `ovk` без чёткого обоснования (см. INCIDENT_RESPONSE.md в upstream)
- Не предлагать изменения в shared skills без cross-tenant impact analysis
- Не проксировать апстрим issue'ы без проверки fork-relevance (часть upstream проблем нас не касается)
