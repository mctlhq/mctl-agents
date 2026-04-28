# 0001. Three-tier skill system (builtin / YAML / remote)

**Status:** accepted
**Date:** 2026-02-20

## Context
Self-healing is needed for different alert types: some are simple and universal (OOMKill → bump memory), others are specific to a particular service/team (recover redis from corrupted RDB), and others require external knowledge (ML-based root cause analysis). A single hardcoded set of Go skills scales poorly.

## Decision
**Three-tier skill registry:**
1. **Builtin Go skills** — compiled into the binary, 9 universal patterns. High stability, updated through release.
2. **YAML skills** — defined in `skills/custom/`, hot-reload without restart. Any team can add a regex pattern + remediation template.
3. **Remote skills** — registered via `POST /api/v1/skills/register`, delegate diagnosis to an external HTTP service. For complex AI/ML or vendor-specific cases.

Skill matching ranked by confidence; circuit breaker auto-disables failing skills.

## Consequences
- **+** Builtin = high quality bar (review + tests + Go strict typing)
- **+** YAML = fast iteration for teams (PR in gitops, not in mctl-agent)
- **+** Remote = extensibility without modifying mctl-agent
- **−** Three entry points — increases the surface area of "what can go wrong"
- **−** Circuit breaker may hide a real problem — alert on disabled skills is needed

## What NOT to propose
- Merging all skills into Go (loss of YAML hot-reload)
- Removing the remote tier — it is the future (mctl-agents may become a remote skill source!)
- Switching to a JS/Python plugin engine — we lose Go performance + type safety
