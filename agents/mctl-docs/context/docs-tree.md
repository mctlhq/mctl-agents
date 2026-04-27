# Snapshot структуры docs.mctl.ai

> Этот файл — рабочий artefact для researcher mctl-docs. Регенерируется руками
> по мере роста репо `mctl-docs/`. Снимок собран `find docs -name "*.md"` +
> первый `# Title` каждого файла. Снят 2026-04-27.

Используй чтобы понять "уже задокументировано или gap" для конкретного user-visible изменения. Названия достаточно — для глубокого сравнения researcher может прочитать конкретный файл из `mctl-docs/docs/<path>` через Read.

## Текущие страницы

| Path | Title |
|---|---|
| `docs/getting-started/index.md` | Getting Started |
| `docs/guides/databases.md` | Database Provisioning |
| `docs/guides/domains.md` | Custom Domains |
| `docs/guides/gitops-workflows.md` | GitOps Workflows |
| `docs/guides/previews.md` | Preview Environments |
| `docs/guides/rollbacks.md` | Rollbacks |
| `docs/guides/scaling.md` | Scaling |
| `docs/guides/services.md` | Service Deployment |
| `docs/guides/tenants.md` | Tenant Management |
| `docs/mcp/connecting.md` | Connecting to MCTL MCP Server |
| `docs/mcp/examples.md` | MCP Examples |
| `docs/mcp/overview.md` | MCP Server Overview |
| `docs/mcp/tools-reference.md` | MCP Tools Reference |
| `docs/platform/architecture.md` | Architecture |
| `docs/platform/components.md` | Components |
| `docs/platform/openclaw.md` | OpenClaw Integration |
| `docs/platform/overview.md` | What is MCTL? |
| `docs/api/index.md` | REST API |
| `docs/security/authentication.md` | Authentication |
| `docs/security/authorization.md` | Authorization |
| `docs/reference/faq.md` | FAQ |
| `docs/reference/glossary.md` | Glossary |
| `docs/reference/troubleshooting.md` | Troubleshooting |
| `docs/index.md` | (landing — без явного `# Title`) |

## Подсказки для маппинга commit → page

| Что меняется в коде | Какая страница вероятно затронута |
|---|---|
| `mctl-api/internal/mcp/...` (новые тулзы, изменение JSON-RPC) | `docs/mcp/tools-reference.md`, `docs/mcp/examples.md` |
| `mctl-api/internal/api/...` (REST endpoints) | `docs/api/index.md` |
| `mctl-api` auth/JWT/OAuth | `docs/security/authentication.md` |
| `mctl-portal` (Backstage scaffolder, plugins) | `docs/platform/components.md`, `docs/getting-started/index.md` |
| `mctl-agent` (новый skill, изменение reaction) | `docs/platform/components.md` (mctl-agent блок), `docs/reference/troubleshooting.md` |
| `mctl-gitops` (новые helm charts, ArgoCD apps, workflow templates) | `docs/guides/gitops-workflows.md`, `docs/reference/` |
| `mctl-openclaw` (новые каналы, skill changes) | `docs/platform/openclaw.md` |
| `mctl-web` (формы, OAuth flow) | `docs/getting-started/index.md` (если затрагивает onboarding) |
| `mctl-agents` (этот репо!) | НЕ документируется в docs.mctl.ai (внутренний инструмент) |

## Что точно НЕ документируется
- Внутренние reactor функции / private packages
- Обновления Go-зависимостей (если не breaking)
- CI tweaks (.github/workflows/...)
- Test refactoring
- Linter fixes
