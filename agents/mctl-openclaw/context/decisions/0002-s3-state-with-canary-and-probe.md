# 0002. S3-backed state + canary + restore-state probe

**Status:** accepted
**Date:** 2026-03-15

## Context
openclaw stores sensitive channel state (auth tokens for WhatsApp Web, Telegram session, iMessage cookies, OAuth refresh tokens, etc.). On a pod restart without restoring this state, channels lose their connection, which for `ovk` means downtime for a real customer.

Previously state lived only in memory + a local volume. Several times we lost auth on rollout.

## Decision
Three layers of protection:

1. **S3 as source of truth**. openclaw periodically syncs auth/sessions to an S3 bucket (per tenant). On pod startup — pull from S3 before opening channels.

2. **s3-sync canary workflow** (Argo CronWorkflow). Every N minutes it checks: the pod actually writes to S3 (a fresh timestamp is present). If the canary fails > N cycles — alert. Before rollout the canary stops, after — restart with a delay (otherwise it spams false alerts).

3. **restore-state readiness probe**. The pod is not marked ready until it confirms that auth/sessions have been restored from S3. ArgoCD waits for ready status before marking the rollout successful.

## Consequences
- **+** Cross-pod restart resilience
- **+** Rollout is safer (the probe catches a broken restore before traffic flows)
- **+** Canary provides early warning of sync problems
- **−** Dependency on S3 (bucket down = pod does not start) — mitigation: backup region
- **−** Canary skips cycles during rollout — must be accounted for in alert thresholds
- **−** Probe timeout must be set higher than the slowest channel takes to restore

## Recurring footguns (from memory)
- Rollout without stopping the canary → false alerts
- Too short a probe timeout → ArgoCD times the pod out even when it is restoring successfully
- Changing S3 bucket policy without checking on all tenants
- Cleaning a bucket "for testing" — auth is lost for live customers

## What NOT to propose (for analyst/researcher)
- Replacing S3 with something stateless (etcd, Redis) without a serious comparison
- Disabling the canary "because it's noisy" — the cause of the noise must be fixed
- Reducing probe timeout without a stress test on the slowest channel
