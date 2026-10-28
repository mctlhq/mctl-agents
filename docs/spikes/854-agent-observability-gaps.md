# Spike #854: agent observability signals DevLoop lacks today

STATUS: ADOPT_SUBSET

Owner decision recorded 2026-10-28: `ADOPT_SUBSET` (allowed: `ADOPT_FOLLOW_UPS`,
`ADOPT_SUBSET`, `RESEARCH_ONLY`, `REJECT`). The adopted subset is F0 and F1 to F3
(G1 to G3), filed now. F4 to F7 (G4 to G7) are deferred and not filed. Refs
mctlhq/mctl-agents#854. Related: mctlhq/mctl-agents#830 has since merged
(`docs/spikes/830-runtime-egress-detection.md`); it covers network egress only
and overlaps none of G1 to G7. mctlhq/mctl-agents#60 (LLM-as-judge evals, open)
would be another producer of the G1 event and should reuse its shape.

This spike does not propose "add tracing". mctl-agents#195 already ships one
OpenTelemetry trace per DevLoop run. This report inventories what that pipeline
does not cover. No runtime behaviour changes in this PR.

## Baseline

Emitted today, described in `docs/observability/execution-traces.md`:

- `invoke_agent` -> `chat <model>` -> `execute_tool <name>` spans, with token
  usage: `orchestrator/tracing.py` `AgentRunObserver`.
- gh/git command spans and `mctl.github.*`: `command_span`, `classify_command`.
- `mctl.artifact.write` events: `record_artifact`.
- `mctl.policy.decision` events: `record_policy_decision`, called from
  `orchestrator/policy_checkpoint.py`.
- Export guard and key allowlist: `orchestrator/tracing_sdk.py`
  (`GuardedExporter`, `redact_attributes`, `key_allowed`).
- The only OpenTelemetry metrics are commerce ones:
  `orchestrator/commerce_metrics.py` (meter `mctl.commerce`).

Deployed state, as read when the design was written (gitops path in each case,
`services/admins/mctl-agents-worker-exec/values.yaml`): image tag `1.107.0`,
`otel.enabled: true`, `MCTL_TRACE_WORKFLOW_TYPES: DevLoopWorkflow`,
`MCTL_TRACE_ARGO_PARAM: "true"`. The `mctl-agents-worker-implement` and
`mctl-agents-worker` values carry the same two variables and an `otel:` block.

Not observed from this run: the investigate/implement CWFT pod env
(`MCTL_TRACE_REQUIRE_PARENT`, `TRACEPARENT` mapping), the Collector redaction
fix (mctl-gitops#1332), live Tempo contents, and whether pod spans and integer
token counters arrive in Tempo. No gap below is inferred from those sources.
Confirming them is the first step of follow-up G5.

## Gap matrix

State is `emitted`, `emitted-elsewhere` or `missing`. Every name in the
"proposed name" column is **proposed**, and needs a mctl-docs catalog
reservation (`docs/reference/telemetry-attributes.md`) before it ships. Upstream
OpenTelemetry GenAI names are recorded where known. Their stability at the
semconv version mctl-docs pins is not verified here and must be checked before
reservation.

| ID | Class | Signal | State | Producer (file, symbol) | Proposed name | Upstream semconv | Priority |
|---|---|---|---|---|---|---|---|
| - | a | Model-call spans | emitted | `orchestrator/tracing.py` `AgentRunObserver` | none | `gen_ai.*` (already used) | - |
| - | b | Tool-call spans | emitted | `orchestrator/tracing.py` `AgentRunObserver` | none | `gen_ai.tool.name` (already used) | - |
| G2 | c | Run outcome: stop reason, terminal reason, turn count | emitted-elsewhere (usage ledger) | `orchestrator/usage_ledger.py` `UsageRecorder`; would be set in `AgentRunObserver._result` | `mctl.agent.stop_reason`, `mctl.agent.terminal_reason`, `mctl.agent.turns` | `gen_ai.response.finish_reasons` for stop reason only; none for the others | P1 |
| G3 | d | Agent RED metrics (rate, errors, duration, tokens, tool errors) | missing | would sit beside `orchestrator/commerce_metrics.py` | `mctl.agent.invocations`, `mctl.agent.invocation.duration`, `mctl.agent.tool.calls`, `mctl.agent.tokens` | `gen_ai.client.token.usage`, `gen_ai.client.operation.duration` exist; names to verify | P1 |
| G1 | e | Evaluation results on the trace (context retrieval, proposal decision, review rounds, PR outcome, CI result) | emitted-elsewhere (`context_eval` records, `.status.yaml`) | `orchestrator/context_eval.py` caller in `run_issue_investigator.py`; DevLoop approval and shepherd activities | `mctl.eval.result` event, `mctl.eval.name`, `mctl.eval.score`, `mctl.eval.label` | `gen_ai.evaluation.*` may exist; to verify | P1 |
| G4 | f | Budget, rate-limit and provider-pressure signals | emitted-elsewhere (policy events only when routed through the checkpoint; spike 791 offline records) | `orchestrator/exec_budget.py` (no tracing calls) | `mctl.budget.threshold`, `mctl.budget.denied` events; `mctl.provider.http_429`, `mctl.provider.retries` on `chat` | none | P2 |
| G5 | g | Coverage outside the traced runners | missing | `run_authoring_canary`, `run_incident_responder`, `run_mentor`, `run_scout`, `run_service_agent` (all call an agent without `tracing.agent_run`); `MCTL_TRACE_WORKFLOW_TYPES` in gitops | none | none | P2 |
| G6 | e | Context strategy fields on spans | emitted-elsewhere (log line) | `orchestrator/context_assembly.py` `CONTEXT_STRATEGY_RELEASE`/`_COMPARE` | `mctl.context.*` on the pod root span | none | P2 |
| G7 | h | Loop timeline: approval and human-input waits | missing | `orchestrator/temporal/dev_loop.py` activities (activity side only, replay-safe) | `mctl.wait.approval.duration`, `mctl.wait.human_input.duration` events | none | P3 |

Evaluation signals are observability only. They never authorize anything,
restating ADR 015's non-authorization rule.

## Redaction check

Checked in a REPL against `orchestrator/tracing_sdk.key_allowed` (key shape
only):

| Name | Passes `key_allowed` |
|---|---|
| `mctl.eval.name` | yes |
| `mctl.eval.score` | yes |
| `mctl.eval.label` | yes |
| `mctl.agent.stop_reason` | yes |
| `mctl.agent.terminal_reason` | yes |
| `mctl.agent.turns` | yes |
| `mctl.eval.result` | no, the guard drops the `result` suffix |

`mctl.eval.result` is therefore used only as the span event name. No attribute
may use that suffix. Names in G4, G6 and G7 follow the same `mctl.<a>.<b>`
shape and must be re-checked when reserved. The remaining rules of "Redaction
rules" in `docs/observability/execution-traces.md` apply to every proposal: no
prompts, completions, tool arguments or results, issue bodies, argv, or monetary
amounts on metrics, and no opt-in to record them. Metric attributes use closed
vocabularies and carry no ids.

## Follow-up issues

Order: the mctl-docs reservation (F0) comes before any emitting change.

### F0. Reserve proposed telemetry names in the catalog
- Tracked: mctlhq/mctl-docs#178.
- Repo: mctl-docs. Priority: P1. Depends on: none.
- Scope: add the G1 to G4, G6 and G7 names to
  `docs/reference/telemetry-attributes.md` with types, closed vocabularies and
  the upstream OpenTelemetry GenAI equivalent where one exists at the pinned
  semconv version (verify `gen_ai.evaluation.*` and the `gen_ai.client.*`
  metrics). Record the redaction constraint for each name.

### F1 (G1). Emit evaluation results as span events
- Tracked: mctlhq/mctl-agents#871.
- Repo: mctl-agents. Priority: P1. Depends on: F0. Overlap check done: none with #830; #60 reuses this event.
- Scope: add a `mctl.eval.result` span event with `mctl.eval.name` (closed
  vocabulary: `context.retrieval`, `proposal.decision`, `pr.review_rounds`,
  `pr.outcome`, `ci.result`), and `mctl.eval.score` and/or `mctl.eval.label`
  (closed). Produce `context.retrieval` from the `context_eval` call in
  `run_issue_investigator.py`, and lifecycle outcomes from the DevLoop approval
  and shepherd/merge activities. Late outcomes (merge, revert) are emitted as a
  span in a new trace linked by `mctl.workflow.id`, since the original trace may
  have closed or the workflow may have continued as new. Default to span events
  over a separate eval store, to be confirmed in review. Tests assert the
  redaction guard keeps every attribute.

### F2 (G2). Run outcome attributes on `invoke_agent`
- Tracked: mctlhq/mctl-agents#872.
- Repo: mctl-agents. Priority: P1. Depends on: F0.
- Scope: in `AgentRunObserver._result`, set `mctl.agent.stop_reason`,
  `mctl.agent.terminal_reason` (bounded closed strings, unknown values map to
  `other`) and `mctl.agent.turns` (int), copying what `UsageRecorder` already
  reads from `ResultMessage`. Extend `tests/test_tracing_agents.py`.

### F3 (G3). Agent RED metrics
- Tracked: mctlhq/mctl-agents#873.
- Repo: mctl-agents. Priority: P1. Depends on: F0.
- Scope: add a `mctl.agents` meter next to `commerce_metrics.py` with
  `mctl.agent.invocations` (agent, outcome, model), `mctl.agent.invocation.duration`
  (histogram), `mctl.agent.tool.calls` (agent, tool bucketed to a closed list,
  status) and `mctl.agent.tokens` (agent, model, token type). Same closed
  attribute and no-ids, no-amounts rule as commerce metrics. Emit for every
  workflow type (metrics carry no ids), unlike `MCTL_TRACE_WORKFLOW_TYPES`.

### F4 (G4). Budget and provider-pressure signals
- Deferred by the owner decision of 2026-10-28; not filed.
- Repo: mctl-agents. Priority: P2. Depends on: F0.
- Scope: emit exec-budget threshold and denial events from
  `orchestrator/exec_budget.py`, and `http_429`/retry counts on `chat` spans.
  No monetary amounts.

### F5 (G5). Trace the remaining agent runners
- Deferred by the owner decision of 2026-10-28; not filed.
- Repo: mctl-agents and mctl-gitops. Priority: P2. Depends on: none.
- Scope: wrap the agent run in `tracing.agent_run` in the runners the registry
  below marks `agent_call: yes, traced: no`, then widen
  `MCTL_TRACE_WORKFLOW_TYPES` per type in gitops. First confirm the unobserved
  deployed facts listed in Baseline. Flip each `traced` value in the registry in
  the same PR.

### F6 (G6). Context strategy fields on spans
- Deferred by the owner decision of 2026-10-28; not filed.
- Repo: mctl-agents. Priority: P2. Depends on: F0.
- Scope: move the log-only `mctl.context.*` fields onto the pod root span, the
  item `execution-traces.md` already names as outstanding.

### F7 (G7). Approval and human-input wait durations
- Deferred by the owner decision of 2026-10-28; not filed.
- Repo: mctl-agents. Priority: P3. Depends on: F0.
- Scope: record wait durations as events from activities only (not workflow
  code), so replay stays deterministic, as `traceparent` already is.

## Coverage registry

Consumed by `tests/test_tracing_coverage.py`. One row per
`orchestrator/run_*.py` module. `agent_call` is reviewed by hand (it means the
module starts an LLM agent run). `traced` is checked against a static `ast`
search for a `tracing.agent_run` call. Every row with `agent_call: yes` and
`traced: no` must be named in a G-row of the gap matrix.

```coverage-registry
runner | agent_call | traced
run_all | no | no
run_authoring_canary | yes | no
run_context_eval | no | no
run_implementer | yes | yes
run_incident_responder | yes | no
run_issue_directive_poller | no | no
run_issue_investigator | yes | yes
run_issue_poller | no | no
run_mentor | yes | no
run_scout | yes | no
run_security_verifier | no | no
run_service_agent | yes | no
run_shepherd | yes | yes
run_token | no | no
run_usage_collector | no | no
```
