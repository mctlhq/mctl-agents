# Spike #852: sandbox isolation delta beyond #807 and #830

STATUS: DRAFT

## 1. Source and limits

- Article: https://thenewstack.io/how-to-sandbox-agents/ ("When 'isolated' doesn't mean isolated: How to
  sandbox AI agents").
- The article body could NOT be retrieved on retry: the fetch returned only the headline and site
  navigation. The taxonomy is therefore derived from the title and publicly known sandboxing categories:
  kernel sharing, network egress, credentials, filesystem persistence, tool mediation, untrusted code
  execution and resource exhaustion. No claim below rests on the article text.
- Not observed: live cluster state. All evidence is repository code, gitops files and the pinned
  `claude-agent-sdk` 0.2.168 package with its bundled CLI.
- PR #810 head SHA the coverage map was built against: `7b1c9267f5cf80d738e3b2f3134415740402d764`
  (checked again at implementation time, unchanged). Re-check against #810's current head before merge.
  #810 is open, so G1-G6 are cited by name as well as number.

## 2. Coverage map

| category | mapping |
|---|---|
| kernel sharing | covered-by-807:G5 (no sandbox runtime) |
| network egress (pod) | covered-by-807:G1 (no egress control), covered-by-830 (egress detection) |
| network egress (provider-side tools) | new: D1 |
| credentials | covered-by-807:G2 (credentials in env); in-process sub-gap: D2/D4 |
| filesystem persistence | not-a-gap: per-pod `emptyDir` in `cwft-mctl-agents-implement.yaml` and `cwft-mctl-agents-investigate.yaml` |
| tool mediation | new: D2 |
| untrusted code execution | covered-by-807:G2/G5; in-process sub-gap: D4 |
| untrusted configuration | new: D3 |
| resource exhaustion | covered-by-807:G6 (no compute quota) |

## 3. Delta gap list

| id | layer | gap | evidence | status | owner_spike | follow_up |
|---|---|---|---|---|---|---|
| D1-WebSearch | tool | `WebSearch` is executed on the provider side: the bundled CLI issues a model API request carrying a `web_search_20250305` server tool, so the query leaves through the model API, not a pod-originated fetch. Pod NetworkPolicy (#807 G1) and #830 egress detection do not see the search target; the query text can carry data. Severity: medium. | `orchestrator/options.py:1304`, `:1377`, `:1527` (allowed_tools); `agent-platform/execution-profiles/implementer-default/profile.yaml` tools; SDK bundled CLI (WebSearch tool, `web_search_20250305`) | new | none | Drop `WebSearch` from profiles that do not need it, or route it through an MCP tool behind the policy checkpoint. Owner: mctl-gitops (profiles) and mctl-agents (`options.py`) |
| D1-WebFetch | tool | `WebFetch` runs locally in the CLI process inside the pod (direct HTTP request with manual redirect handling, plus a domain-allow check call to the model vendor's API host). Its traffic is in the pod network path, so it is governed by #807 G1 and observable under #830. No separate boundary gap. | SDK bundled CLI (WebFetch implementation, `redirect: manual`, domain check); `orchestrator/options.py:1304` | covered-by-807 | #807 G1 | none |
| D2 | tool | `Bash` is audit-only: `_audit_pre_tool_use` prints the command and returns `{}`; `_policy_hooks` matches only `mcp__.*`. No deny path exists for shell at the application layer. Severity: medium (pod controls remain the backstop). | `orchestrator/options.py:_command_audit_hooks` (line 614), `:_policy_hooks` (line 1071) | new | none | Policy-mediated, credential-separated `Bash`: model shell runs without long-lived credentials in its env; credentialed operations go through a mediated helper or MCP tool behind the policy checkpoint. A command deny-list may be added only as defence in depth, never as the control. Owner: mctl-agents |
| D3 | config | Several builders use `setting_sources=["project"]` with cwd set to a target-repository clone, so configuration is read from untrusted repository content. Per the pinned SDK docs, `"project"` covers `.claude/settings.json` (which can carry permissions and hooks) in addition to CLAUDE.md, agents and skills; the code comment at `options.py:293-295` already states agents are loaded from the target repo. Severity: high if repository-supplied settings hooks execute (the CLI documents project settings as a settings layer, so treat as likely); not exercised live. | `orchestrator/options.py:293-295`, `:1307`, `:1380`, `:1399`, `:1483`, `:1531`; `claude_agent_sdk/types.py` `ClaudeAgentOptions.setting_sources` docstring | new | none | Use `setting_sources=[]` for target-repository clones and load personas from the image, or allowlist read `.claude` paths. Owner: mctl-agents |
| D4 | code-exec | Repository code run by the model through `Bash` (builds, tests, installs) inherits the full pod environment. Only the type-check child is env-scrubbed. Credentials-in-env itself is #807 G2; the sub-gap is not applying the same allowlist to model `Bash`. | `orchestrator/run_implementer.py:_TYPECHECK_ENV_KEEP` (line 4269) | covered-by-807 | #807 G2 | Folded into the D2 follow-up (scrubbed env for model shell) |
| D5 | input | Untrusted issue and PR text enters prompts; the effective boundary is the capability ceiling D1-D3. | `orchestrator/run_issue_investigator.py` prompt construction | not-observed | none | none separate; mitigated by D1-D3 fixes. Prompt-level mitigation efficacy was not tested |
| K1 | kernel | Shared node kernel | #807 G5 | covered-by-807 | #807 G5 | none |
| N1 | network | Pod egress and package-install escape | #807 G1, #830 | covered-by-830 | #830 | none |
| F1 | fs | Cross-run filesystem persistence | `argo-workflows/cluster-templates/cwft-mctl-agents-implement.yaml` (`emptyDir`, "no shared workdir volume") | not-a-gap | none | none |
| R1 | resources | Quota and fan-out | #807 G6 | covered-by-807 | #807 G6 | none |

## 4. Decision

Recommended token: `NEW_GAPS_FOLLOW_UP` (D1-WebSearch, D2 and D3 are `new`). The report merges as
`STATUS: DRAFT` until the owner records one of `NO_NEW_GAPS`, `NEW_GAPS_FOLLOW_UP`, `FOLD_INTO_807`.
Whether to close #852 as a duplicate is the owner's call.

## 5. Follow-ups (draft issue bodies, not filed)

1. **Remove provider-side WebSearch from profiles that do not need it** (mctl-gitops profiles and
   mctl-agents `options.py`). Control: drop the grant, or mediate via an MCP tool. Rollback: restore the
   tool in the profile.
2. **Policy-mediated, credential-separated Bash** (mctl-agents). Control: scrubbed env for model shell,
   credentialed operations behind a mediated helper under the policy checkpoint; deny-list only as defence
   in depth. Rollback: revert the builder change.
3. **Stop loading project settings from target-repository clones** (mctl-agents). Control:
   `setting_sources=[]` for target clones with personas shipped in the image. Rollback: revert to
   `["project"]`.
