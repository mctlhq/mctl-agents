# 0001. Three separate tenants instead of a single multi-user deployment

**Status:** accepted
**Date:** 2026-02-01

## Context
openclaw supports a multi-user gateway, but its state (auth tokens, channel sessions, S3-bucket scoping) is hard to safely isolate within one instance. The mctl team operates openclaw for:
- internal needs (`admins`)
- experiments with beta features (`labs`)
- a production customer with a high SLA (`ovk`)

Merging into a single deployment would mean the blast radius of all changes lands on the production customer.

## Decision
**Three independent deployments** of openclaw in separate Kubernetes namespaces (`admins`, `labs`, `ovk`). Each has:
- its own S3 bucket for state
- its own helm release in mctl-gitops
- its own rollout pipeline
- shared **3-layer skills** (built-in + YAML + remote) to reduce duplication

Changes roll out in order: `labs` → (observation for N days) → `admins` → `ovk`.

## Consequences
- **+** Full isolation of state and blast radius
- **+** `labs` serves as a canary for `ovk`
- **+** `ovk` SLA does not depend on team experiments
- **−** 3x the resources
- **−** 3x the operational tasks (rollouts, monitoring, tech debt)
- **−** Skills across the trio are synchronized by hand (when shared)

## What NOT to propose (for analyst/researcher)
- Merging the tenants into a single deployment — explicitly rejected
- Removing `labs` to save resources — it is critical as a canary for `ovk`
- A direct rollout to `ovk` without going through `labs`
