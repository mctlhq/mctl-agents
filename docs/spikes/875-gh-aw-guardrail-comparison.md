# Spike #875: GitHub Agentic Workflows guardrails compared with DevLoop

STATUS: DRAFT

## 1. Source and limits

- GA changelog (2026-10-28), as retrieved during investigation: read-only default permissions, safe
  outputs, sandboxed container behind the Agent Workflow Firewall (AWF) with an optional micro-VM, a
  threat detection job.
- Safe-outputs reference: only the first 100k of about 183k characters were retrieved. Retrieved claims:
  the agent holds no write permission, separate permission-scoped jobs execute the agent's requests,
  per-type `max` limits, body validation, label filters.
- NOT retrieved: AWF network rules, threat detection job internals, integrity filter rules, staged mode.
  Rows depending on them are `not-observed` and are not used to justify `adopt`.
- Not observed: live cluster state. DevLoop evidence is repository code and gitops files (symbol names
  are cited so line drift does not invalidate them).
- Owner decision is not recorded yet; the report stays `DRAFT` until it is.

## 2. Canonical boundaries

gh-aw is a source of patterns only. These remain canonical and nothing is replaced by gh-aw: Temporal and
Argo, the ADR 014 policy checkpoint, ExecutionProfiles in mctl-gitops `agent-platform/`, ADR 013 human
input, ADR 016 and ADR 021 approval and completion, and ADR 018 evidence.

## 3. Comparison

| id | dimension | gh_aw_pattern | devloop_today | status | owner | follow_up |
|---|---|---|---|---|---|---|
| S1 | safe outputs | Agent emits structured requests; a separate job holding permissions executes them | `orchestrator/run_issue_investigator.py` `_staging_dir`, `_landed_triplet_defects`, `post_proposal_comment`; `orchestrator/run_implementer.py` push and `_open_pr_for_branch` are orchestrator code | already-covered | mctl-agents | none |
| S2 | safe outputs | Per-output-type `max` and content validation (body bounds, placeholder rejection, label filters) | No count limit or content check on proposal comments, PR creation or human-input drafts: `post_proposal_comment`, `_open_pr_for_branch`, `orchestrator/human_input.py` `_reject_unknown_keys` (keys only) | adopt | mctl-agents | F1: orchestrator output limiter routed through the ADR 014 checkpoint as a rule. Control: per-type caps and content validation before comment, PR and draft acceptance. Rollback: revert the rule and limiter module; outputs return to today's behavior |
| S3 | safe outputs | Staged mode with no side effects | `trigger_shepherd` and `reconcile` have `dry_run`; the investigator has none | not-observed | mctl-agents | none |
| P1 | permissions | Agent job holds no write credential | `GITHUB_TOKEN` from secret `mctl-agents-secrets` key `github-token` sits in the run container: mctl-gitops `argo-workflows/cluster-templates/cwft-mctl-agents-investigate.yaml`; `MCTL_TOKEN` already removed (#717) | covered-by-852 | mctl-gitops | #869 (D2/D4) |
| P2 | permissions | Read-only default for tools | Profile tools broad with `permission_mode="acceptEdits"` in `orchestrator/options.py` `build_issue_investigator_options`; MCP default-deny in `_policy_hooks` and `orchestrator/policy_checkpoint.py` `decide()` | covered-by-852 | mctl-agents | #868, #869 |
| P3 | permissions | Same as P1, as an explicit target | No stated target that the agent container holds only read-only GitHub credentials; P1 tracks the gap but not this target | adopt | mctl-gitops | F3: add the target "agent container holds read-only GitHub credentials" as a note on #869. Control: comment only, no new issue. Rollback: delete the comment |
| N1 | network | AWF domain allowlist | Squid `http_access allow all` in mctl-gitops `infra-components/mctl-platform/mctl-agents-egress-proxy/proxy.yaml`; no agents egress NetworkPolicy under `argo-workflows/config/`; profile `network: approved-providers-only` unenforced | covered-by-807 | mctl-gitops | #807 G1, #830 |
| N2 | sandbox | Optional micro-VM | Shared node kernel; Argo per-pod CWFTs in `argo-workflows/cluster-templates/` | covered-by-807 | mctl-gitops | #807 G5 |
| N3 | network | Provider-side tools outside the firewall | `WebSearch` in `allowed_tools` (`orchestrator/options.py`) executes on the provider side and bypasses pod egress | covered-by-852 | mctl-agents | #870 |
| T1 | threat detection | Detection job scans proposed changes between agent and output jobs | Review workflows `.github/workflows/claude-review.yml`, `agy-review.yml`, `security.yml` and the ADR 016 gate run after the PR exists; nothing scans before publish. gh-aw detection internals not retrieved | not-observed | mctl-agents | none until the internals are read |
| R1 | reusable enforcement | Declarative frontmatter compiled to a locked workflow | ExecutionProfile plus mctl-gitops `agent-platform/policy.yaml` (fail closed on unknown references) | already-covered | mctl-gitops | none |
| I1 | interoperability | gh-aw workflow calling mctl through Actions OIDC | Deploy endpoint accepts Actions OIDC; no MCP path (`.github/workflows/` deploy job pattern) | not-applicable | mctl-api | none; recorded only |
| I2 | interoperability | Bring-your-own engine | Claude Agent SDK pinned in `pyproject.toml` | not-applicable | mctl-agents | none |

## 4. Decision

Not recorded. Expected owner choice: `ADOPT_PATTERNS`, scoped to S2 and P3 (allowed: `ADOPT_PATTERNS`,
`REFERENCE_ONLY`, `NO_ACTION`). Adopt rows: S2, P3.

## 5. Follow-ups

To be filed by the owner after the decision. Gaps already tracked (#807, #830, #852, #868-#870) are cited
above and not duplicated.

- F1 (S2), owner mctl-agents: output limiter as an ADR 014 checkpoint rule; rollback by reverting it.
- F3 (P3), owner mctl-gitops: note on #869; rollback by deleting the comment.
- T1: re-evaluate a pre-publish output scan only after the gh-aw threat detection pages are read.
