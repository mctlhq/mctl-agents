# mctl platform map (scout context)

Use this to decide which repository an article is relevant to. Repo = mctlhq/<name>.

- mctl-api: platform API and MCP server (api.mctl.ai); tenants, operations, lifecycle ownership, Temporal.
- mctl-gitops: ArgoCD/Argo Workflows manifests, ClusterWorkflowTemplates, Vault/ExternalSecrets, Cloudflare tofu.
- mctl-agents: DevLoop agents (investigator, implementer, shepherd), orchestrator, agent registry and profiles.
- mctl-agent: webhook service behind agent.mctl.ai (the MCTL_AI bot).
- mctl-telegram: Telegram interface to the platform.
- mctl-portal: customer portal and signup.
- mctl-web: public website.
- mctl-docs: documentation (docs.mctl.ai).
- mctl-alice: Alice voice/assistant interface.
- newton-mcp-gateway: MCP gateway.
- .github: org-wide roadmap, epics and SpikeDefinitions (roadmap/spikes). Use when no component fits.

Current priorities:
- Agent safety: sandboxing, prompt injection, least-privilege tool policy, budgets.
- MCP ecosystem: protocol changes, auth, gateways, enterprise MCP.
- Kubernetes/ArgoCD/Argo Workflows operations, CI supply-chain security.
- Claude/Anthropic model and Agent SDK changes (cost, new models, limits).
- GitHub platform changes affecting PR/issue automation and Actions.
- Cheap multi-tenant hosting: resource limits, networking, observability.
