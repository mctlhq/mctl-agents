# 0001. VitePress 1.6 for the documentation portal

**Status:** accepted
**Date:** 2026-03-28

## Context
Until 2026-03 the mctl platform did not have a dedicated documentation portal. Documentation migrated between different places (README in the mctl-web repo, the `/docs` page on mctl.ai). With the growing number of services (mctl-api, mctl-portal, mctl-agent, mctl-openclaw, etc.), this fragmentation began to hinder onboarding of new tenants and partners.

## Decision
A separate `mctl-docs` repo was created on **VitePress 1.6** with serving via `docs.mctl.ai`. Stack:
- VitePress 1.6 (Vue 3 under the hood, SSG output)
- mermaid 11 for diagrams
- TypeScript for configs
- nginx + Docker → mctl-gitops → ArgoCD deploy

## Consequences
- **+** One URL for all user-facing documentation
- **+** SSG = fast serving, SEO-friendly
- **+** Markdown + Vue components when needed
- **+** Easy to contribute (PR to a .md file)
- **−** Build step (~30 sec for full rebuild)
- **−** No built-in versioning — old doc versions are lost on upgrade
- **−** Mermaid adds ~200KB to bundle

## What NOT to propose (for the analyst/spec-writer of the mctl-docs agent)
- Replacing VitePress with Docusaurus / MkDocs / GitBook without a strong rationale (migration is expensive, ROI unclear).
- Introducing i18n before the platform has a non-English audience.
- Complex custom Vue components instead of standard markdown — raises the contribution bar.
