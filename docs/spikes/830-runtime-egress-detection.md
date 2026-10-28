# Spike #830: runtime observability and egress controls that detect an agent escaping its sandbox

STATUS: DRAFT

## 1. Source and limits

- Motivating case: the public description of an OpenAI evaluation in which an agent's package
  installation reached the internet. The article body was not retrieved by the investigating run, so
  the taxonomy rests on that public description plus repository and gitops evidence.
- Not observed: live cluster state. Nothing below was confirmed against a running pod, Loki or Squid.
- Evidence re-checked at implementation time: `orchestrator/options.py` `_audit_pre_tool_use` (line 566)
  and `_command_audit_hooks` (line 614) and `_policy_hooks` (line 1071) match the proposal.
- Gitops evidence re-checked on mctl-gitops `main` on 2026-10-28 (paths under `platform-gitops/`):
  - mctl-gitops#2161 (stage A1, proxy) merged 2026-10-26; #2163 (stage A2, routing) merged
    2026-10-26 and re-landed with a wait-for-proxy guard in #2165. The stage table in
    `docs/runbooks/mctl-agents-egress.md` still lists A2 as "#2163 (draft)"; that row is stale.
    Stages B and C are "not started".
  - `infra-components/mctl-platform/mctl-agents-egress-proxy/proxy.yaml`: `access_log stdio:/dev/stdout
    squid` (native format, no `%un`) and `http_access allow all` after the `Safe_ports`, `CONNECT`,
    link-local and loopback denies.
  - `HTTPS_PROXY` occurrences per `argo-workflows/cluster-templates/cwft-mctl-agents-*.yaml`: approve 4,
    daily 0, implement 5, investigate 5, reconcile 5, run 5, scout 2, shepherd 6, usage-collector 2.
    The daily template does not route through the proxy, so its egress falls under X2 even in stage A.
  - `argo-workflows/config/` has `networkpolicy.yaml` (ingress) and no
    `networkpolicy-agents-egress.yaml`, consistent with stage C not started.
- #810 head SHA the preventive-primitive coverage was built against:
  `7b1c9267f5cf80d738e3b2f3134415740402d764`; re-checked 2026-10-28, PR still open (draft) at that head.
- Related reports: `docs/spikes/852-sandbox-isolation-delta.md` assigns pod egress detection to this
  spike (row N1) and notes provider-side search is invisible to pod controls (D1-WebSearch,
  D1-WebFetch). `docs/spikes/854-agent-observability-gaps.md` has no egress gap. #807 G1 (no egress
  control on agent pods) and G2 (credentials in env) are the preventive counterparts.

### Not observed

Each of these is unknown, not absent:

- the live Squid access log in Loki (no read-only tool reaches the `mctl-agents-egress` namespace);
- whether the Loki ruler is enabled;
- kube-router drop logging;
- the CoreDNS `log` plugin;
- Argo `init`/`wait` executor egress;
- the discrepancy between `mctl_get_workflow_logs` and Promtail `argo-workflows` ingestion;
- the live pod environment (`HTTPS_PROXY` present or not); the gitops templates are merged, the
  running pods were not inspected.

## 2. Escape-path matrix

State vocabulary: `emitted | partial | missing | blind | out-of-scope`. Telemetry proposed here uses
closed vocabularies only: no argv, model-chosen hostnames or tool arguments as attributes or labels
(see "Redaction rules" in `docs/observability/execution-traces.md`).

| id | path | layer | signal | producer | attribution | state | unblocked_by | follow_up |
|---|---|---|---|---|---|---|---|---|
| X1 | Proxied HTTP(S) from model tools, package managers and curl that honour `HTTPS_PROXY` (#852 N1) | network | Squid access log on stdout, shipped to Loki | `infra-components/mctl-platform/mctl-agents-egress-proxy/proxy.yaml` | client pod IP only; no run attribution | partial | stage A2 live; F2 for attribution; stage B for denials | F1, F2 |
| X2 | Direct connection bypassing the proxy (`env -u HTTPS_PROXY`, raw socket, tool ignoring env) (#852 N1) | network | none before stage C; after C refused connections and kube-router drops (not observed) | `argo-workflows/config/networkpolicy-agents-egress.yaml` (not yet created) | pod IP / pod name after C | blind | stage C, then F4, F5 | F4, F5 |
| X3 | DNS side channel (kube-dns stays allowed in stage C) | network | CoreDNS query log if the `log` plugin is enabled (not observed) | CoreDNS config in mctl-gitops | pod IP | blind | F6 | F6 |
| X4 | Provider-side `WebSearch` (#852 D1-WebSearch) | tool | `execute_tool WebSearch` span count only; destination never visible | `orchestrator/tracing.py` `AgentRunObserver` | execution context id via trace | partial | #852 follow-up 1 (remove or mediate) | none (owned by #852) |
| X5 | Argo `init`/`wait` executors, direct to R2 and kube API | platform | none; excluded from stage C by the runbook | `docs/runbooks/mctl-agents-egress.md` | none; not agent-controlled | out-of-scope | runbook task 2 | none |
| X6 | In-process intent: model runs an install, fetch or proxy-unsetting command | process | `AUDIT tool=Bash cmd=<repr> execution_context=<id>` line; free text, no counter, no alert | `orchestrator/options.py` `_audit_pre_tool_use` | execution context id | partial | F3 | F3 |
| X7 | Repository code at build or test time (postinstall, setup.py) opening sockets | process | same as X1 or X2 depending on proxy use; no process identity | none (would need an eBPF sensor, deferred) | pod IP only | blind | eBPF sensor (deferred) | none |

Cross-references: X1, X2 and X4 cite #852 (rows N1 and D1-WebSearch / D1-WebFetch). D1-WebFetch runs
in the pod and is therefore covered by X1/X2. #807 G1 is closed by the staged proxy; #807 G2 raises
the impact of every undetected path. #854 has no egress gap.

X2 cannot be detected at the network layer before stage C is enforced: until a default-deny egress
NetworkPolicy exists, a direct connection succeeds and leaves no network-layer trace.

## 3. Egress policy and runtime audit plan

Ordered by the proxy rollout stages in `docs/runbooks/mctl-agents-egress.md` (mctl-gitops).

- **Stage A (now; A1 allow-all with logging, A2 CWFTs routed through the proxy).** F1: Promtail
  counters on the Squid log (`agent_egress_requests_total`, `agent_egress_log_lines_total`) following
  the vault-audit pattern, plus VMRules `AgentEgressProxyLogSilent` and `AgentEgressProxyLogAbsent`
  (a detector that silently stops is worse than none). F2: run attribution via a per-run proxy
  username (workflow name, never a credential) and a Squid `logformat` with `%un`; fallback is a
  pod-IP join. F3: a closed classifier on the `Bash` audit hook adding `class=` to the AUDIT line with
  vocabulary `package-install`, `network-fetch`, `proxy-env-change`, `none`; only the class token is
  emitted, never argv. `proxy-env-change` alerts at once.
- **Stage B (hostname allowlist).** F4: counter `agent_egress_denied_total` on `TCP_DENIED/403` lines
  and alert `AgentEgressDenied`. Hostnames stay in Loki and are never metric labels. Package
  registries decision: implementer and test runs legitimately need `pypi.org`,
  `files.pythonhosted.org`, `registry.npmjs.org` and `proxy.golang.org`. Recommendation: allow them
  for implementer CWFTs only; long-term, a pull-through registry mirror removes them from the
  allowlist.
- **Stage C (enforce).** The egress NetworkPolicy turns X2 from blind into refused. F5: a scheduled
  canary CronWorkflow labelled `app.kubernetes.io/name: mctl-agents` makes one direct connection with
  the proxy unset and one proxied request to a non-allowlisted host; it passes only if both are
  refused and `agent_egress_denied_total` moved. F6 covers DNS and is P3.
- **Residual risk (accepted, owner decides).** Allowing public package registries in stage B leaves
  package-upload exfiltration and dependency-confusion fetches possible. Provider-side `WebSearch`
  (X4) stays invisible to any pod control.

## 4. Decision

STATUS stays `DRAFT` until the owner records a token. Recommended token: `ADOPT_PLAN` (allowed:
`ADOPT_PLAN`, `ADOPT_SUBSET`, `RESEARCH_ONLY`). This PR changes no runtime, gitops or profile state
and files no issues.

### F1 Squid log counters and silence alerts

- Repo: mctl-gitops
- Priority: P1
- Dependency: stage A2 live
- Scope: Promtail metrics stage on `mctl-agents-egress` Squid log; two VMRules with promtool tests.
- Rollback: revert the Promtail and VMRule changes; no runtime path depends on them.

### F2 Run attribution in the Squid log

- Repo: mctl-gitops
- Priority: P1
- Dependency: F1
- Scope: per-run proxy username in `HTTPS_PROXY` in the agent CWFTs; Squid accept-any-user helper and
  `logformat` with `%un`.
- Rollback: remove the username from `HTTPS_PROXY` and the helper; attribution falls back to pod IP.

### F3 Bash audit command classifier

- Repo: mctl-agents
- Priority: P2
- Dependency: none
- Scope: closed-vocabulary `class=` token on the AUDIT line in `_audit_pre_tool_use`; no argv copied;
  never denies. Promtail counter per class lives in mctl-gitops.
- Rollback: revert the commit; the AUDIT line returns to its current shape.

### F4 Denied-request counter and alert

- Repo: mctl-gitops
- Priority: P1
- Dependency: stage B
- Scope: `agent_egress_denied_total` and `AgentEgressDenied`, routed to Telegram; also covers refused
  direct connections after stage C if drop logging is available.
- Rollback: revert the rule and Promtail stage.

### F5 Egress detector canary

- Repo: mctl-gitops
- Priority: P2
- Dependency: stage C, F4
- Scope: scheduled CronWorkflow with a direct and a proxied forbidden connection; passes only if both
  are refused and the denied counter moved.
- Rollback: suspend and delete the CronWorkflow.

### F6 DNS visibility

- Repo: mctl-gitops
- Priority: P3
- Dependency: stage C
- Scope: enable CoreDNS `log` for agent namespaces or use a restricted resolver; counters only, no
  query names as labels.
- Rollback: revert the CoreDNS config change.
