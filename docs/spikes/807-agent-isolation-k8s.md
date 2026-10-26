# Spike #807: Kubernetes isolation primitives for non-deterministic agents

STATUS: DRAFT

The decision token (one of `NO_CHANGE`, `HARDEN_IN_PLACE`, `DEDICATED_AGENT_NAMESPACE`,
`SANDBOX_RUNTIME_REQUIRED`) is the #807 owner's call. This report proposes `HARDEN_IN_PLACE`.

## Environment and versions

- Audited: mctl-gitops `platform-gitops/` at commit `8a06919a3109b836bdb0ea554f29355b780a637b`.
- Method: static read of gitops YAML with `tools/agent_isolation_audit.py` (offline, read-only).
  Reproduce with `python tools/agent_isolation_audit.py --gitops-root <platform-gitops> --format md`.
- Not observed (no live cluster access in this spike): the CNI and whether it supports FQDN egress,
  the live labels of the `argo-workflows` namespace, actual pod specs.

## Primitive catalogue

Threat: a model-driven `Bash` tool runs on input the platform does not control (issue text, PR diffs,
review comments). Assume a prompt-injected agent can run any shell command inside its pod.

| Primitive | Defends against |
|---|---|
| RBAC / SA token | Using the Kubernetes API from the pod. |
| NetworkPolicy egress | Exfiltration of credentials or data to arbitrary hosts. |
| NetworkPolicy east-west | Reaching platform pods (Vault, mctl-api, Argo) from the agent pod. |
| Pod Security (PSA + securityContext) | Privilege escalation, writable root filesystem. |
| Sandbox runtime (gVisor/Kata) | Container escape through the shared node kernel. |
| ResourceQuota / LimitRange / limits / deadlines | Resource exhaustion and unbounded fan-out. |
| Credential handling | Tokens in env vars are readable by the agent shell. |

## Current-state matrix

Statuses: `enforced`, `partial`, `absent`, `not-observed`, `declared-not-enforced`. The full per-template
output of the auditor has one row per agent step (7 CWFTs); the table below is the consolidated view.

| Primitive | Status | Severity | Source | Field | Note |
|---|---|---|---|---|---|
| RBAC / SA token | enforced | high | argo-workflows/config/sa-and-rbac.yaml | `mctl-agents-runner`, `automountServiceAccountToken` | No binding for the runner SA; executor SA limited to `workflowtaskresults`. |
| Egress NetworkPolicy | absent | high | argo-workflows/config/networkpolicy.yaml | `spec.policyTypes` | No Egress policy selects `app.kubernetes.io/name: mctl-agents`. |
| Profile `permissions.network` | declared-not-enforced | high | agent-platform/execution-profiles/implementer-default/profile.yaml | `spec.permissions.network` | Same for all 8 profiles that declare `approved-providers-only`. |
| East-west ingress | partial | medium | argo-workflows/config/networkpolicy.yaml | `spec.ingress[].from[].podSelector` | `podSelector: {}` admits every pod in the namespace. |
| Cross-namespace reachability | partial | medium | bootstrap/templates/core-infra/vault-netpol.yaml | `spec.ingress[].from[].namespaceSelector` | Vault, backstage and mctl-api admit the whole `argo-workflows` namespace. |
| Pod Security, per container | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `runAsNonRoot`, `seccompProfile`, `allowPrivilegeEscalation`, `drop: [ALL]` | Present on every agent step the auditor found. |
| `readOnlyRootFilesystem` | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `container.securityContext` | Unset in all agent CWFTs. |
| Namespace PSA label | not-observed | medium | bootstrap/templates/core-infra/argo-workflows.yaml | namespace metadata | No label in gitops; live labels not observed. |
| Sandbox runtime | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `runtimeClassName` | Also absent for every other agent CWFT. |
| Compute quota / LimitRange | absent | low | argo-workflows/config/local-workdir-quota.yaml | `spec.hard` | Only a storage-class quota exists. |
| Per-pod limits and deadline | partial | low | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `resources`, `activeDeadlineSeconds` | Set on implement; several other agent templates have no template-level deadline or mutex (a spec-level deadline may apply). |
| Credential exposure | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `env[].valueFrom.secretKeyRef` | Tokens injected as env vars (names only reported by the auditor). |
| CNI / FQDN egress support | not-observed | high | (none found in gitops) | n/a | Determines policy vs proxy for the allowlist. |

## Ranked gaps

1. **G1 (high)**: no egress control, `approved-providers-only` declared but not enforced.
   Primitive: default-deny egress NetworkPolicy for `app.kubernetes.io/name: mctl-agents`, FQDN policy or egress proxy.
2. **G2 (high with G1, medium without)**: long-lived tokens in env vars. Primitive: short-lived file-mounted
   tokens (existing `GITHUB_TOKEN_FILE` pattern), later a credential-injecting proxy.
3. **G3 (medium)**: shared namespace, namespace-wide ingress admits. Primitive: narrower ingress selectors, or a dedicated namespace.
4. **G4 (medium)**: no namespace PSA enforcement found, no `readOnlyRootFilesystem`.
5. **G5 (medium, owner decision)**: no sandbox runtime. Primitive: gVisor `RuntimeClass` (check Go `-race`, Node, git).
6. **G6 (low)**: no compute ResourceQuota / LimitRange.

RBAC is the one primitive already enforced; keep `mctl-agents-runner` binding-free.

## Layering with app-layer controls

USD budgets (ADR 011, `orchestrator/exec_budget.py`), the MCP policy checkpoint (ADR 014) and the
capability gateway (ADR 017) are in-process controls. A model-driven `Bash` runs in the same container and
is not mediated by them, so they do not replace the pod-level primitives above.

## Follow-ups

All target mctl-gitops unless noted. None is applied by this spike.

- (a) **Default-deny egress for agent pods.** Files: new `argo-workflows/config/networkpolicy-agents-egress.yaml`.
  Expected traffic: DNS, mctl-api, otel-collector, Temporal if needed, api.anthropic.com, github.com,
  npm/PyPI/Go module proxy (implementer test runs). Needs the CNI answer to pick FQDN policy vs proxy.
  Rollback: delete the policy.
- (b) **Env tokens to short-lived file-mounted or proxy-injected credentials.** Files: the agent CWFTs,
  `mctl-agents` entrypoint. Rollback: restore the `secretKeyRef` env entries.
- (c) **East-west narrowing.** Files: `argo-workflows/config/networkpolicy.yaml`,
  `bootstrap/templates/core-infra/vault-netpol.yaml`, `mctl-api-netpol.yaml`, `backstage-netpol.yaml`.
  Rollback: revert the selectors (platform workflows need Vault and mctl-api access, so test deploy-service first).
- (d) **PSA `restricted` via a dedicated agent namespace, plus `readOnlyRootFilesystem`.** Files: namespace
  manifest, agent CWFTs. Platform helper steps run as root, so not on `argo-workflows` itself. Rollback: revert the namespace label.
- (e) **ResourceQuota / LimitRange for agent pods.** Size from per-pod limits times max concurrency. Rollback: delete the objects.
- (f) **gVisor RuntimeClass evaluation.** Node pool and `runtimeClassName` on one agent CWFT first. Rollback: remove `runtimeClassName`.
- (g) **Run the auditor in mctl-gitops CI.** Rollback: remove the CI step.

Optional owner-run probe (not run here): a throwaway pod labelled `app.kubernetes.io/name: mctl-agents`
in `argo-workflows` that attempts outbound HTTPS to an arbitrary host and a TCP connect to Vault.

## Open questions

- Which CNI, and does it support FQDN egress?
- Live labels of the `argo-workflows` namespace (PSA).
- Do the Temporal workers (`services/admins/mctl-agents-worker*`) ever run a model-driven shell in-process?
- Legitimate egress inventory for the implementer.
- Overlap with #747, #760 and #670 (not readable offline).

## Decision

STATUS: DRAFT. Proposed: `HARDEN_IN_PLACE`, with `SANDBOX_RUNTIME_REQUIRED` as a separate later decision.
