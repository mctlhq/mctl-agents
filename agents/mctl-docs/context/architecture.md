# Architecture: mctl-docs

## Purpose
Public documentation portal `docs.mctl.ai`. Source of truth for platform documentation of mctl: getting-started, guides, MCP, API reference, security, platform internals.

## Tech stack
- **VitePress 1.6** (Vue-based static site generator)
- **mermaid 11** for diagrams (enabled via the VitePress plugin)
- **TypeScript** (tsconfig.json) — for configs and the custom theme
- Build: `vitepress build docs` → `docs/.vitepress/dist/`
- Serving: nginx + Dockerfile (see `mctl-docs/Dockerfile`, `mctl-docs/nginx.conf`)
- Deploy: via mctl-gitops → ArgoCD

## `docs/` structure (VitePress root)

| Folder | Purpose |
|---|---|
| `docs/.vitepress/` | Site config: `config.{ts,mts}`, theme overrides, sidebar/nav |
| `docs/getting-started/` | Onboarding, "first 5 minutes" tutorial, first service |
| `docs/guides/` | How-to articles (deploy a service, secrets, custom domain, etc.) |
| `docs/platform/` | Platform concepts (tenants, ArgoCD flow, Backstage) |
| `docs/mcp/` | mctl-api MCP server: tool list, OAuth flow, examples |
| `docs/api/` | REST API reference for mctl-api |
| `docs/security/` | Auth models, secrets, vault, threat model |
| `docs/reference/` | Helm chart templates, configmaps, conventions |
| `docs/public/` | Static assets (logos, og-image) |

> A detailed snapshot of the current structure (with short annotations on each page) is in `context/docs-tree.md`.

## What we do NOT document
- Internal implementation details of individual repos (each repo has its own CLAUDE.md)
- Development commands (live-reload, debug) — those go in the README of the corresponding repo
- Code review process (PR convention, codex review) — in `.claude/CLAUDE.md` of the repo

## External integrations
- **mctl-api** — the main source of truth for `docs/api/` and `docs/mcp/`
- **mctl-portal (Backstage)** — for `docs/platform/backstage*.md`
- **mctl-gitops** — for `docs/guides/gitops*.md`, `docs/reference/helm*.md`

## Conventions for the doc agent
- **Frontmatter** — VitePress supports YAML frontmatter (title, description, layout). Use it for overrides.
- **Cross-links** — root-relative, no extension (e.g. `[MCP overview](/mcp/overview)`).
- **Code blocks** — specify the language (` ```bash`, ` ```yaml`).
- **Mermaid** — ` ```mermaid` for flow / sequence / state diagrams.
- **Tenant slang** — everywhere `admins/labs/ovk` lowercase, as on the platform.
- **English only for user-facing documentation** (although CLAUDE.md in this repo is in Russian, like the rest of the mctl-agents code).

## Known limitations
- VitePress 1.6 — the latest 2.x is not in use; a bump on update must account for breaking changes.
- The build is single-section (one `docs/`), no concept of versioned docs (old versions are lost on upgrade).
- Mermaid bundles add ~200KB to bundle size.

## Mctl MCP (for the researcher to verify prod versions of services)
The `mcp__mctl__*` tools may return current versions of mctl-api / mctl-web / etc. — this is critical for documenting only what the user can actually use NOW, not a PR in a feature branch.
