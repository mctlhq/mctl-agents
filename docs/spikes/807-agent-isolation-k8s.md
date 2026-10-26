# Spike #807: Kubernetes isolation primitives for non-deterministic agents

STATUS: DRAFT

The decision token (one of `NO_CHANGE`, `HARDEN_IN_PLACE`, `DEDICATED_AGENT_NAMESPACE`,
`SANDBOX_RUNTIME_REQUIRED`) is the #807 owner's call. This report proposes `HARDEN_IN_PLACE`.

## Environment and versions

- Audited: mctl-gitops `platform-gitops/` at commit `f6727392c4e7bd843a86eea77896fd555417bc79` (origin/main, 2026-10-26).
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
output of the auditor has one row per agent step (7 agent steps across 8 CWFT files) and is captured verbatim
under [Auditor output](#auditor-output); the table below is the consolidated view.

| Primitive | Status | Severity | Source | Field | Note |
|---|---|---|---|---|---|
| RBAC / SA token | enforced | high | argo-workflows/config/sa-and-rbac.yaml | `mctl-agents-runner`, `automountServiceAccountToken` | No binding for the runner SA; executor SA limited to `workflowtaskresults`. |
| Egress NetworkPolicy | absent | high | argo-workflows/config/networkpolicy.yaml | `spec.policyTypes` | No Egress policy selects `app.kubernetes.io/name: mctl-agents`. |
| Profile `permissions.network` | declared-not-enforced | high | agent-platform/execution-profiles/implementer-default/profile.yaml | `spec.permissions.network` | Same for all 8 profiles that declare `approved-providers-only`. |
| East-west ingress | partial | medium | argo-workflows/config/networkpolicy.yaml | `spec.ingress[].from[].podSelector` | `podSelector: {}` admits every pod in the namespace. |
| Cross-namespace reachability | partial | medium | bootstrap/templates/core-infra/vault-netpol.yaml | `spec.ingress[].from[].namespaceSelector` | Vault, backstage, mctl-api, shared-pg and otel-collector admit the whole `argo-workflows` namespace. |
| Templated NetworkPolicies | not-observed | medium | helm-charts/tenant/templates/networkpolicy.yaml | `kind: NetworkPolicy` | 3 Helm/Go-templated files are not statically parseable; none names `argo-workflows` as its namespace. |
| Pod Security, per container | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `runAsNonRoot`, `seccompProfile`, `allowPrivilegeEscalation`, `drop: [ALL]` | Present on every agent step the auditor found. |
| `readOnlyRootFilesystem` | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `container.securityContext` | Unset in all agent CWFTs. |
| Namespace PSA label | not-observed | medium | bootstrap/templates/core-infra/argo-workflows.yaml | namespace metadata | No label in gitops; live labels not observed. |
| Sandbox runtime | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `runtimeClassName` | Also absent for every other agent CWFT. |
| Compute quota / LimitRange | absent | low | argo-workflows/config/local-workdir-quota.yaml | `spec.hard` | Only a storage-class quota exists. |
| Per-pod limits and deadline | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `resources`, `activeDeadlineSeconds` | cpu/memory/ephemeral-storage requests and limits on all 7 agent steps; every CWFT has a spec-level deadline. |
| Fan-out mutex / semaphore | absent | low | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | `synchronization` | Present on implement only; absent on the other 6 agent steps. |
| Credential exposure | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | `env[].valueFrom.secretKeyRef` | Tokens injected as env vars (names only reported by the auditor). |
| CNI / FQDN egress support | not-observed | high | (none found in gitops) | n/a | Determines policy vs proxy for the allowlist. |

## Ranked gaps

1. **G1 (high)**: no egress control, `approved-providers-only` declared but not enforced.
   Primitive: default-deny egress NetworkPolicy for `app.kubernetes.io/name: mctl-agents`, FQDN policy or egress proxy.
   Tracked in mctlhq/mctl-gitops#2159 (stage A, an egress proxy, is in flight).
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

- (a) **Default-deny egress for agent pods.** Tracked in mctlhq/mctl-gitops#2159 (stage A proxy in flight). Files: new `argo-workflows/config/networkpolicy-agents-egress.yaml`.
  Expected traffic: DNS, mctl-api, otel-collector, Temporal if needed, api.anthropic.com, github.com,
  npm/PyPI/Go module proxy (implementer test runs). Needs the CNI answer to pick FQDN policy vs proxy.
  Rollback: delete the policy.
- (b) **Env tokens to short-lived file-mounted or proxy-injected credentials.** Files: the agent CWFTs,
  `mctl-agents` entrypoint. Rollback: restore the `secretKeyRef` env entries.
- (c) **East-west narrowing.** Files: `argo-workflows/config/networkpolicy.yaml`,
  `bootstrap/templates/core-infra/vault-netpol.yaml`, `mctl-api-netpol.yaml`, `backstage-netpol.yaml`,
  `infra-components/data/cnpg/shared/networkpolicy.yaml`, `infra-components/observability/otel-collector/networkpolicy.yaml`.
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

## Auditor output

Captured verbatim from `python tools/agent_isolation_audit.py --gitops-root <platform-gitops> --format md`
against mctl-gitops `f6727392c4e7bd843a86eea77896fd555417bc79`. Env vars are reported by name only.

<details>
<summary>Full per-template matrix (110 rows)</summary>

| primitive | scope | status | severity | source | field | detail | recommendation |
|---|---|---|---|---|---|---|---|
| RBAC/ServiceAccount | mctl-agents-implement/run-implementer | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | serviceAccountName | serviceAccountName='mctl-agents-runner' (template), expected 'mctl-agents-runner' | Use mctl-agents-runner on agent steps. |
| RBAC/ServiceAccount | mctl-agents-implement/run-implementer | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | automountServiceAccountToken | value=False (template), expected False | Set automountServiceAccountToken: false. |
| RBAC/ServiceAccount | mctl-agents-implement/run-implementer | enforced | high | argo-workflows/config/sa-and-rbac.yaml | RoleBinding/ClusterRoleBinding subjects | mctl-agents-runner has no binding | Keep mctl-agents-runner binding-free. |
| PodSecurity | mctl-agents-implement/run-implementer | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | securityContext.runAsNonRoot | securityContext.runAsNonRoot: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-implement/run-implementer | enforced | medium | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | securityContext.seccompProfile.type | securityContext.seccompProfile.type: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-implement/run-implementer | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | container.securityContext.allowPrivilegeEscalation | container.securityContext.allowPrivilegeEscalation: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-implement/run-implementer | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | container.securityContext.capabilities.drop | container.securityContext.capabilities.drop: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-implement/run-implementer | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | container.securityContext.readOnlyRootFilesystem | readOnlyRootFilesystem not set | Mount writable emptyDirs and set readOnlyRootFilesystem: true. |
| SandboxRuntime | mctl-agents-implement/run-implementer | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | runtimeClassName | runtimeClassName=None | Evaluate a gVisor/Kata RuntimeClass on a labelled node pool. |
| Resources | mctl-agents-implement/run-implementer | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | container.resources | missing: none | Set cpu/memory/ephemeral requests and limits. |
| Resources | mctl-agents-implement/run-implementer | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | activeDeadlineSeconds | activeDeadlineSeconds=7200 (template) | Bound every agent step. |
| Resources | mctl-agents-implement/run-implementer | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | synchronization | mutex/semaphore present (template) | Bound fan-out with a mutex or semaphore. |
| CredentialExposure | mctl-agents-implement/run-implementer | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml | container.env[].valueFrom.secretKeyRef | secret env vars readable by the agent shell: CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, MCTL_EVIDENCE_WRITER_TOKEN, MCTL_TOKEN, MCTL_USAGE_WRITER_TOKEN | Short-lived file-mounted or proxy-injected credentials. |
| RBAC/ServiceAccount | mctl-agents-investigate/run-investigator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | serviceAccountName | serviceAccountName='mctl-agents-runner' (template), expected 'mctl-agents-runner' | Use mctl-agents-runner on agent steps. |
| RBAC/ServiceAccount | mctl-agents-investigate/run-investigator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | automountServiceAccountToken | value=False (template), expected False | Set automountServiceAccountToken: false. |
| RBAC/ServiceAccount | mctl-agents-investigate/run-investigator | enforced | high | argo-workflows/config/sa-and-rbac.yaml | RoleBinding/ClusterRoleBinding subjects | mctl-agents-runner has no binding | Keep mctl-agents-runner binding-free. |
| PodSecurity | mctl-agents-investigate/run-investigator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | securityContext.runAsNonRoot | securityContext.runAsNonRoot: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-investigate/run-investigator | enforced | medium | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | securityContext.seccompProfile.type | securityContext.seccompProfile.type: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-investigate/run-investigator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | container.securityContext.allowPrivilegeEscalation | container.securityContext.allowPrivilegeEscalation: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-investigate/run-investigator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | container.securityContext.capabilities.drop | container.securityContext.capabilities.drop: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-investigate/run-investigator | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | container.securityContext.readOnlyRootFilesystem | readOnlyRootFilesystem not set | Mount writable emptyDirs and set readOnlyRootFilesystem: true. |
| SandboxRuntime | mctl-agents-investigate/run-investigator | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | runtimeClassName | runtimeClassName=None | Evaluate a gVisor/Kata RuntimeClass on a labelled node pool. |
| Resources | mctl-agents-investigate/run-investigator | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | container.resources | missing: none | Set cpu/memory/ephemeral requests and limits. |
| Resources | mctl-agents-investigate/run-investigator | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | activeDeadlineSeconds | activeDeadlineSeconds=7200 (spec) | Bound every agent step. |
| Resources | mctl-agents-investigate/run-investigator | absent | low | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | synchronization | mutex/semaphore absent (unset) | Bound fan-out with a mutex or semaphore. |
| CredentialExposure | mctl-agents-investigate/run-investigator | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml | container.env[].valueFrom.secretKeyRef | secret env vars readable by the agent shell: CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, MCTL_EVIDENCE_WRITER_TOKEN, MCTL_USAGE_WRITER_TOKEN | Short-lived file-mounted or proxy-injected credentials. |
| RBAC/ServiceAccount | mctl-agents-reconcile/run-reconcile | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | serviceAccountName | serviceAccountName='mctl-agents-runner' (template), expected 'mctl-agents-runner' | Use mctl-agents-runner on agent steps. |
| RBAC/ServiceAccount | mctl-agents-reconcile/run-reconcile | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | automountServiceAccountToken | value=False (template), expected False | Set automountServiceAccountToken: false. |
| RBAC/ServiceAccount | mctl-agents-reconcile/run-reconcile | enforced | high | argo-workflows/config/sa-and-rbac.yaml | RoleBinding/ClusterRoleBinding subjects | mctl-agents-runner has no binding | Keep mctl-agents-runner binding-free. |
| PodSecurity | mctl-agents-reconcile/run-reconcile | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | securityContext.runAsNonRoot | securityContext.runAsNonRoot: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-reconcile/run-reconcile | enforced | medium | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | securityContext.seccompProfile.type | securityContext.seccompProfile.type: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-reconcile/run-reconcile | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | container.securityContext.allowPrivilegeEscalation | container.securityContext.allowPrivilegeEscalation: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-reconcile/run-reconcile | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | container.securityContext.capabilities.drop | container.securityContext.capabilities.drop: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-reconcile/run-reconcile | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | container.securityContext.readOnlyRootFilesystem | readOnlyRootFilesystem not set | Mount writable emptyDirs and set readOnlyRootFilesystem: true. |
| SandboxRuntime | mctl-agents-reconcile/run-reconcile | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | runtimeClassName | runtimeClassName=None | Evaluate a gVisor/Kata RuntimeClass on a labelled node pool. |
| Resources | mctl-agents-reconcile/run-reconcile | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | container.resources | missing: none | Set cpu/memory/ephemeral requests and limits. |
| Resources | mctl-agents-reconcile/run-reconcile | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | activeDeadlineSeconds | activeDeadlineSeconds=1800 (spec) | Bound every agent step. |
| Resources | mctl-agents-reconcile/run-reconcile | absent | low | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | synchronization | mutex/semaphore absent (unset) | Bound fan-out with a mutex or semaphore. |
| CredentialExposure | mctl-agents-reconcile/run-reconcile | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-reconcile.yaml | container.env[].valueFrom.secretKeyRef | secret env vars readable by the agent shell: GITHUB_TOKEN | Short-lived file-mounted or proxy-injected credentials. |
| RBAC/ServiceAccount | mctl-agents-run/run-orchestrator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | serviceAccountName | serviceAccountName='mctl-agents-runner' (template), expected 'mctl-agents-runner' | Use mctl-agents-runner on agent steps. |
| RBAC/ServiceAccount | mctl-agents-run/run-orchestrator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | automountServiceAccountToken | value=False (template), expected False | Set automountServiceAccountToken: false. |
| RBAC/ServiceAccount | mctl-agents-run/run-orchestrator | enforced | high | argo-workflows/config/sa-and-rbac.yaml | RoleBinding/ClusterRoleBinding subjects | mctl-agents-runner has no binding | Keep mctl-agents-runner binding-free. |
| PodSecurity | mctl-agents-run/run-orchestrator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | securityContext.runAsNonRoot | securityContext.runAsNonRoot: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-run/run-orchestrator | enforced | medium | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | securityContext.seccompProfile.type | securityContext.seccompProfile.type: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-run/run-orchestrator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | container.securityContext.allowPrivilegeEscalation | container.securityContext.allowPrivilegeEscalation: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-run/run-orchestrator | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | container.securityContext.capabilities.drop | container.securityContext.capabilities.drop: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-run/run-orchestrator | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | container.securityContext.readOnlyRootFilesystem | readOnlyRootFilesystem not set | Mount writable emptyDirs and set readOnlyRootFilesystem: true. |
| SandboxRuntime | mctl-agents-run/run-orchestrator | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | runtimeClassName | runtimeClassName=None | Evaluate a gVisor/Kata RuntimeClass on a labelled node pool. |
| Resources | mctl-agents-run/run-orchestrator | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | container.resources | missing: none | Set cpu/memory/ephemeral requests and limits. |
| Resources | mctl-agents-run/run-orchestrator | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | activeDeadlineSeconds | activeDeadlineSeconds=3600 (spec) | Bound every agent step. |
| Resources | mctl-agents-run/run-orchestrator | absent | low | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | synchronization | mutex/semaphore absent (unset) | Bound fan-out with a mutex or semaphore. |
| CredentialExposure | mctl-agents-run/run-orchestrator | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-run.yaml | container.env[].valueFrom.secretKeyRef | secret env vars readable by the agent shell: CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, MCTL_TOKEN | Short-lived file-mounted or proxy-injected credentials. |
| RBAC/ServiceAccount | mctl-agents-scout/scout | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | serviceAccountName | serviceAccountName='mctl-agents-runner' (template), expected 'mctl-agents-runner' | Use mctl-agents-runner on agent steps. |
| RBAC/ServiceAccount | mctl-agents-scout/scout | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | automountServiceAccountToken | value=False (template), expected False | Set automountServiceAccountToken: false. |
| RBAC/ServiceAccount | mctl-agents-scout/scout | enforced | high | argo-workflows/config/sa-and-rbac.yaml | RoleBinding/ClusterRoleBinding subjects | mctl-agents-runner has no binding | Keep mctl-agents-runner binding-free. |
| PodSecurity | mctl-agents-scout/scout | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | securityContext.runAsNonRoot | securityContext.runAsNonRoot: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-scout/scout | enforced | medium | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | securityContext.seccompProfile.type | securityContext.seccompProfile.type: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-scout/scout | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | container.securityContext.allowPrivilegeEscalation | container.securityContext.allowPrivilegeEscalation: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-scout/scout | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | container.securityContext.capabilities.drop | container.securityContext.capabilities.drop: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-scout/scout | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | container.securityContext.readOnlyRootFilesystem | readOnlyRootFilesystem not set | Mount writable emptyDirs and set readOnlyRootFilesystem: true. |
| SandboxRuntime | mctl-agents-scout/scout | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | runtimeClassName | runtimeClassName=None | Evaluate a gVisor/Kata RuntimeClass on a labelled node pool. |
| Resources | mctl-agents-scout/scout | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | container.resources | missing: none | Set cpu/memory/ephemeral requests and limits. |
| Resources | mctl-agents-scout/scout | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | activeDeadlineSeconds | activeDeadlineSeconds=1200 (spec) | Bound every agent step. |
| Resources | mctl-agents-scout/scout | absent | low | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | synchronization | mutex/semaphore absent (unset) | Bound fan-out with a mutex or semaphore. |
| CredentialExposure | mctl-agents-scout/scout | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-scout.yaml | container.env[].valueFrom.secretKeyRef | secret env vars readable by the agent shell: CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, SCOUT_TELEGRAM_BOT_TOKEN, SCOUT_TELEGRAM_CHAT_ID | Short-lived file-mounted or proxy-injected credentials. |
| RBAC/ServiceAccount | mctl-agents-shepherd/run-shepherd | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | serviceAccountName | serviceAccountName='mctl-agents-runner' (template), expected 'mctl-agents-runner' | Use mctl-agents-runner on agent steps. |
| RBAC/ServiceAccount | mctl-agents-shepherd/run-shepherd | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | automountServiceAccountToken | value=False (template), expected False | Set automountServiceAccountToken: false. |
| RBAC/ServiceAccount | mctl-agents-shepherd/run-shepherd | enforced | high | argo-workflows/config/sa-and-rbac.yaml | RoleBinding/ClusterRoleBinding subjects | mctl-agents-runner has no binding | Keep mctl-agents-runner binding-free. |
| PodSecurity | mctl-agents-shepherd/run-shepherd | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | securityContext.runAsNonRoot | securityContext.runAsNonRoot: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-shepherd/run-shepherd | enforced | medium | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | securityContext.seccompProfile.type | securityContext.seccompProfile.type: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-shepherd/run-shepherd | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | container.securityContext.allowPrivilegeEscalation | container.securityContext.allowPrivilegeEscalation: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-shepherd/run-shepherd | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | container.securityContext.capabilities.drop | container.securityContext.capabilities.drop: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-shepherd/run-shepherd | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | container.securityContext.readOnlyRootFilesystem | readOnlyRootFilesystem not set | Mount writable emptyDirs and set readOnlyRootFilesystem: true. |
| SandboxRuntime | mctl-agents-shepherd/run-shepherd | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | runtimeClassName | runtimeClassName=None | Evaluate a gVisor/Kata RuntimeClass on a labelled node pool. |
| Resources | mctl-agents-shepherd/run-shepherd | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | container.resources | missing: none | Set cpu/memory/ephemeral requests and limits. |
| Resources | mctl-agents-shepherd/run-shepherd | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | activeDeadlineSeconds | activeDeadlineSeconds=7200 (spec) | Bound every agent step. |
| Resources | mctl-agents-shepherd/run-shepherd | absent | low | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | synchronization | mutex/semaphore absent (unset) | Bound fan-out with a mutex or semaphore. |
| CredentialExposure | mctl-agents-shepherd/run-shepherd | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-shepherd.yaml | container.env[].valueFrom.secretKeyRef | secret env vars readable by the agent shell: CLAUDE_CODE_OAUTH_TOKEN, GITHUB_TOKEN, MCTL_EVIDENCE_WRITER_TOKEN, MCTL_TOKEN, MCTL_USAGE_WRITER_TOKEN | Short-lived file-mounted or proxy-injected credentials. |
| RBAC/ServiceAccount | mctl-agents-usage-collector/collect-usage | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | serviceAccountName | serviceAccountName='mctl-agents-runner' (template), expected 'mctl-agents-runner' | Use mctl-agents-runner on agent steps. |
| RBAC/ServiceAccount | mctl-agents-usage-collector/collect-usage | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | automountServiceAccountToken | value=False (template), expected False | Set automountServiceAccountToken: false. |
| RBAC/ServiceAccount | mctl-agents-usage-collector/collect-usage | enforced | high | argo-workflows/config/sa-and-rbac.yaml | RoleBinding/ClusterRoleBinding subjects | mctl-agents-runner has no binding | Keep mctl-agents-runner binding-free. |
| PodSecurity | mctl-agents-usage-collector/collect-usage | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | securityContext.runAsNonRoot | securityContext.runAsNonRoot: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-usage-collector/collect-usage | enforced | medium | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | securityContext.seccompProfile.type | securityContext.seccompProfile.type: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-usage-collector/collect-usage | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | container.securityContext.allowPrivilegeEscalation | container.securityContext.allowPrivilegeEscalation: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-usage-collector/collect-usage | enforced | high | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | container.securityContext.capabilities.drop | container.securityContext.capabilities.drop: ok | Set the restricted-profile value. |
| PodSecurity | mctl-agents-usage-collector/collect-usage | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | container.securityContext.readOnlyRootFilesystem | readOnlyRootFilesystem not set | Mount writable emptyDirs and set readOnlyRootFilesystem: true. |
| SandboxRuntime | mctl-agents-usage-collector/collect-usage | absent | medium | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | runtimeClassName | runtimeClassName=None | Evaluate a gVisor/Kata RuntimeClass on a labelled node pool. |
| Resources | mctl-agents-usage-collector/collect-usage | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | container.resources | missing: none | Set cpu/memory/ephemeral requests and limits. |
| Resources | mctl-agents-usage-collector/collect-usage | enforced | low | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | activeDeadlineSeconds | activeDeadlineSeconds=900 (spec) | Bound every agent step. |
| Resources | mctl-agents-usage-collector/collect-usage | absent | low | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | synchronization | mutex/semaphore absent (unset) | Bound fan-out with a mutex or semaphore. |
| CredentialExposure | mctl-agents-usage-collector/collect-usage | partial | high | argo-workflows/cluster-templates/cwft-mctl-agents-usage-collector.yaml | container.env[].valueFrom.secretKeyRef | secret env vars readable by the agent shell: GITHUB_TOKEN, MCTL_USAGE_WRITER_TOKEN | Short-lived file-mounted or proxy-injected credentials. |
| NetworkPolicy/egress | argo-workflows | absent | high | argo-workflows/config/networkpolicy.yaml | spec.policyTypes+podSelector+metadata.namespace | no Egress policy in argo-workflows selects app.kubernetes.io/name=mctl-agents | Default-deny egress for agent pods with an FQDN or proxy allowlist. |
| NetworkPolicy/east-west | argo-workflows | partial | medium | argo-workflows/config/networkpolicy.yaml | spec.ingress[].from[].podSelector | ingress admits every pod in the namespace (agent -> platform pod reachable) | Refuse agent-to-non-agent pod traffic. |
| NetworkPolicy/cross-namespace | bootstrap/templates/observability/eval-namespace.yaml | not-observed | medium | bootstrap/templates/observability/eval-namespace.yaml | kind: NetworkPolicy | templated YAML, not statically parseable | Audit the rendered manifest. |
| NetworkPolicy/cross-namespace | helm-charts/tenant/templates/networkpolicy.yaml | not-observed | medium | helm-charts/tenant/templates/networkpolicy.yaml | kind: NetworkPolicy | templated YAML, not statically parseable | Audit the rendered manifest. |
| NetworkPolicy/cross-namespace | helm-charts/tenant/templates/preview.yaml | not-observed | medium | helm-charts/tenant/templates/preview.yaml | kind: NetworkPolicy | templated YAML, not statically parseable | Audit the rendered manifest. |
| NetworkPolicy/cross-namespace | allow-vault-ingress | partial | medium | bootstrap/templates/core-infra/vault-netpol.yaml | spec.ingress[].from[].namespaceSelector | admits the whole argo-workflows namespace, agent pods included | Narrow the selector to non-agent pods. |
| NetworkPolicy/cross-namespace | allow-backstage-ingress | partial | medium | bootstrap/templates/mctl-platform/backstage-netpol.yaml | spec.ingress[].from[].namespaceSelector | admits the whole argo-workflows namespace, agent pods included | Narrow the selector to non-agent pods. |
| NetworkPolicy/cross-namespace | allow-mctl-api-ingress | partial | medium | bootstrap/templates/mctl-platform/mctl-api-netpol.yaml | spec.ingress[].from[].namespaceSelector | admits the whole argo-workflows namespace, agent pods included | Narrow the selector to non-agent pods. |
| NetworkPolicy/cross-namespace | allow-shared-pg-ingress | partial | medium | infra-components/data/cnpg/shared/networkpolicy.yaml | spec.ingress[].from[].namespaceSelector | admits the whole argo-workflows namespace, agent pods included | Narrow the selector to non-agent pods. |
| NetworkPolicy/cross-namespace | allow-otel-collector-ingress | partial | medium | infra-components/observability/otel-collector/networkpolicy.yaml | spec.ingress[].from[].namespaceSelector | admits the whole argo-workflows namespace, agent pods included | Narrow the selector to non-agent pods. |
| ResourceQuota | argo-workflows | absent | low | argo-workflows/config/local-workdir-quota.yaml | spec.hard | compute/pod quota keys: none | Add pods/limits quota and a LimitRange on the agent namespace. |
| NetworkPolicy/profile | authoring-canary-default | declared-not-enforced | high | agent-platform/execution-profiles/authoring-canary-default/profile.yaml | spec.permissions.network | profile declares network='approved-providers-only'; egress check is absent | Back the declaration with an egress NetworkPolicy. |
| NetworkPolicy/profile | implementer-default | declared-not-enforced | high | agent-platform/execution-profiles/implementer-default/profile.yaml | spec.permissions.network | profile declares network='approved-providers-only'; egress check is absent | Back the declaration with an egress NetworkPolicy. |
| NetworkPolicy/profile | incident-responder-default | declared-not-enforced | high | agent-platform/execution-profiles/incident-responder-default/profile.yaml | spec.permissions.network | profile declares network='approved-providers-only'; egress check is absent | Back the declaration with an egress NetworkPolicy. |
| NetworkPolicy/profile | issue-investigator-default | declared-not-enforced | high | agent-platform/execution-profiles/issue-investigator-default/profile.yaml | spec.permissions.network | profile declares network='approved-providers-only'; egress check is absent | Back the declaration with an egress NetworkPolicy. |
| NetworkPolicy/profile | mentor-default | declared-not-enforced | high | agent-platform/execution-profiles/mentor-default/profile.yaml | spec.permissions.network | profile declares network='approved-providers-only'; egress check is absent | Back the declaration with an egress NetworkPolicy. |
| NetworkPolicy/profile | scout-default | declared-not-enforced | high | agent-platform/execution-profiles/scout-default/profile.yaml | spec.permissions.network | profile declares network='approved-providers-only'; egress check is absent | Back the declaration with an egress NetworkPolicy. |
| NetworkPolicy/profile | service-agent-default | declared-not-enforced | high | agent-platform/execution-profiles/service-agent-default/profile.yaml | spec.permissions.network | profile declares network='approved-providers-only'; egress check is absent | Back the declaration with an egress NetworkPolicy. |
| NetworkPolicy/profile | shepherd-default | declared-not-enforced | high | agent-platform/execution-profiles/shepherd-default/profile.yaml | spec.permissions.network | profile declares network='approved-providers-only'; egress check is absent | Back the declaration with an egress NetworkPolicy. |

</details>

## Decision

STATUS: DRAFT. Proposed: `HARDEN_IN_PLACE`, with `SANDBOX_RUNTIME_REQUIRED` as a separate later decision.
