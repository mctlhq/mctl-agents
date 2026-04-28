# 0001. Backstage as developer portal

**Status:** accepted
**Date:** 2026-01-15

## Context
The platform needs a self-service portal for teams: service onboarding, k8s status viewer, observability dashboards in one place, scaffolder for template generation. Alternatives: Port, Cortex, Compass, custom Vue/React app.

## Decision
**Backstage** (open-source, Spotify origin) with custom plugins for platform specifics (observability, openclaw integration, mctl-api proxy).

## Consequences
- **+** Rich plugin ecosystem (kubernetes, techdocs, scaffolder, github, search)
- **+** Active community, regular releases every 2 weeks
- **+** Open-source, no vendor lock-in
- **+** Yarn workspaces — easy to add custom plugins
- **−** Monorepo weight (yarn build/install are noticeable)
- **−** Backstage major bumps require re-validating each plugin
- **−** Permissions framework — steep learning curve

## What NOT to propose (for analyst/researcher)
- Migrating from Backstage to a SaaS alternative (Port/Cortex/Compass) — loss of data + plugins
- A full rewrite to a custom React app — Backstage solves 80% for free
- Bumping Backstage major immediately on release — community-plugins compat usually lags by 1-2 weeks
