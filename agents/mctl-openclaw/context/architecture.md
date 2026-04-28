# Architecture: mctl-openclaw

## Purpose
Deployment of [openclaw](https://github.com/openclaw/openclaw) (multi-channel AI gateway) on the mctl platform. Three parallel tenant instances with independent state, a shared 3-layer skills architecture, and shared canary/probe protection.

## Tech stack
- **Node.js** + TypeScript (workspace packages)
- **openclaw 2026.3.14** (see `current-version.md`) — upstream `github.com/openclaw/openclaw`, MIT
- **Plugin SDK**: extensions live in `extensions/*`, import `openclaw/plugin-sdk/*`
- **Channels**: WhatsApp (web), Telegram, Discord, Slack, Signal, iMessage, BlueBubbles, Matrix, MS Teams, IRC, LINE, Mattermost, Nextcloud Talk, Nostr, Synology Chat, Tlon, Twitch, Zalo (+Personal), WeChat, QQ, WebChat, Feishu, Google Chat
- **Serving**: Mintlify docs at `docs.openclaw.ai`, installers at `openclaw.ai/install*`
- **Build / deploy**: Docker → mctl-gitops → ArgoCD

## Tenants on mctl
Three openclaw deployments in Kubernetes:

### `admins` (admins tenant)
- System deployment for the mctlhq team
- Full set of channels
- Lowest blast radius (internal)

### `labs` (labs tenant)
- Experimental deployment (new features, beta extensions)
- **Close to the memory limit** — any footprint increase requires justification
- Used to run new channels through before promotion to `ovk`

### `ovk` (ovk tenant)
- Production deployment for a specific customer
- High SLA, restarts are painful
- Changes only after a run-through in `labs`

## Shared 3-layer skills architecture
The skills layout repeats in each tenant:
1. **Layer 1: Built-in skills** (compiled into openclaw core)
2. **Layer 2: YAML skills** (hot-reload from `skills/custom/`)
3. **Layer 3: Remote skills** (HTTP-delegated, registered via REST API)

When updating a skill in one tenant, it is updated across the trio (if shared). Separate skills are possible via tenant-specific overlays in gitops.

## State guards
From team memory (see `reference_openclaw_state_persistence.md`):

### s3-sync canary
A periodic workflow checks that openclaw is actually writing to S3:
- If the canary misses > N cycles → alert
- On rollout: the canary is stopped for the duration of rollout, after which it is restarted with a delay

### restore-state probe
Readiness probe in the pod checks that openclaw has restored sessions/auth from S3:
- If the probe does not pass within timeout → ArgoCD does not mark the rollout successful
- Especially important for `ovk` (we cannot lose auth for production customers)

## Where state lives
- Auth tokens / sessions — S3 (cross-pod restart resilience)
- Channel cookies (WhatsApp web, Telegram session) — S3 + memory
- Skill metrics — SQLite in pod (lost on restart, not critical)
- Conversation history — each channel its own way (see `extensions/<channel>/`)

## External integrations
- **mctl-api** via MCP (`api.mctl.ai/mcp`) — deployment status, metrics
- **Mintlify** — docs.openclaw.ai
- **GitHub openclaw/openclaw** — upstream, fork-tracking

## Dependencies for researcher (release tracking)
The researcher monitors these sources via `WebFetch`:

- `openclaw/openclaw` releases — main upstream
- `openclaw/openclaw` issues — especially `bug` + `security` labels
- `openclaw/nix-openclaw` — Nix packaging
- Channels (if APIs change):
  - `whiskeysockets/Baileys` — WhatsApp Web reverse-engineered
  - `discordjs/discord.js`
  - `slackapi/node-slack-sdk`
- Node.js LTS releases — major bumps require validation
- TypeScript releases — strict type checks

## Known limitations / footguns
- **`labs` tenant** — do not push changes that increase RAM there. Even by 50MB.
- **Channel-agnostic refactors** — affect **all** built-in + extension channels; need to verify routing/allowlist/pairing/onboarding/docs (see CLAUDE.md of the upstream repo)
- **`workspace:*` in plugin dependencies** — breaks `npm install --omit=dev`. Use `peerDependencies`/`devDependencies` for core
- **CODEOWNERS-restricted paths** — do not edit without an explicit request from the owner
- **rollout without canary** — risk of silent loss of S3-sync, found only when pods lose auth

## What NOT to do (for analyst/researcher)
- Do not propose restarts of `ovk` without a clear justification (see INCIDENT_RESPONSE.md in upstream)
- Do not propose changes to shared skills without a cross-tenant impact analysis
- Do not proxy upstream issues without checking fork-relevance (some upstream problems do not concern us)
