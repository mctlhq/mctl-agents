# Execution traces

The DevLoop's execution trace (mctlhq/mctl-agents#195): one OpenTelemetry trace per
Temporal workflow run, running from the Temporal activity through the Argo workflow
into the agent pod, down to each model turn, tool call, GitHub mutation, artifact
and policy decision.

- Code: `orchestrator/tracing.py` (the stdlib-only facade),
  `orchestrator/tracing_sdk.py` (the SDK pipeline and the redaction guard),
  `orchestrator/temporal/tracing.py` (the worker interceptors).
- Tests: `tests/test_tracing.py`, `tests/test_tracing_temporal.py`,
  `tests/test_tracing_agents.py`.
- Attribute names follow the mctl-docs catalog,
  `docs/reference/telemetry-attributes.md`. Names this page introduces that the
  catalog does not list yet are marked **proposed** below. They need a
  reservation PR in mctl-docs before #195 can close. See the checklist.

## Status

The code is in and tested offline. **Nothing is exported in production yet.**
Tracing stays inert until a worker or pod has the standard `OTEL_*` endpoint
variables. The platform Collector (mctl-gitops#902) currently has no trace backend
(`backendEndpoint: ""`, debug exporter only), and choosing one is
mctl-gitops#1280. The live-evidence checklist at the end lists what has to happen,
in order.

## The span tree

```text
<virtual root: hash(workflow_id, run_id)>                  never exported, see below
└── RunActivity:submit_and_wait                            temporalio TracingInterceptor
    └── argo.workflow mctl-agents-investigate              orchestrator/temporal/activities/argo.py
        └── issue-investigator.run                         pod root, parent = TRACEPARENT
            ├── github.issue.view                          gh/git command spans
            ├── git.clone
            ├── invoke_agent issue-investigator            one SDK client session
            │   ├── chat claude-opus-5                     one per model message
            │   ├── execute_tool Read
            │   ├── execute_tool mcp__mctl__get_service_status
            │   └── execute_tool Task                      a sub-agent...
            │       └── chat claude-opus-5                 ...whose turns nest under it
            ├── github.issue.comment
            └── (events) mctl.artifact.write requirements.md | design.md | tasks.md | .status.yaml
                         mctl.policy.decision
```

Every other activity of the same run (`RunActivity:resolve_agent_release`,
`RunActivity:record_execution`, the approve and implement submits) hangs off the
same virtual root. The implement pod's tree has the same shape under
`implementer.run`, with `git.commit`, `git.push` and `github.pr.create`.

### Why a virtual root

The DevLoop is started by a Temporal schedule or by the issue poller, never under a
client span. `TracingInterceptor` creates workflow spans, and propagates to
activities, only when such a parent exists. `always_create_workflow_spans=True`
would mint random ids per workflow task, and the SDK documents that these become
orphans after replay. A loop that lives for days across worker restarts would
split into one trace per restart.

So the worker derives the trace id and a root span id from
`sha256(workflow_id, run_id)` (`tracing.workflow_trace_ids`). Any worker process
gets the same trace for the same run, with no state to lose and no change to
workflow code. The cost is that no process exports a span with the root id, so a
backend shows the trace's root as "not received". A `continue_as_new` starts a new
run id and therefore a new trace. `mctl.workflow.id` joins them.

The interceptors are registered on the **worker**, not the client. The client also
starts DevLoops from the poller's activity, and a client-side interceptor would
parent every DevLoop under that poll tick's trace.

### Replay safety

Workflow code is untouched. The only code that runs in the workflow sandbox is
temporalio's own `TracingWorkflowInboundInterceptor`, which emits nothing while
replaying and schedules no commands. `tests/test_tracing_temporal.py` replays every
recorded history in `tests/fixtures/histories/`, both pre-patch and patched, with
the interceptors installed. The histories are not re-recorded.
`tests/test_workflow_replay.py` is unchanged and green.

`traceparent` is added to the operation parameters inside the activity, not in the
workflow, so the recorded activity input is unchanged.

## Attributes

"Where" names the span that carries the attribute.

| Attribute | Where | Status | Source |
|---|---|---|---|
| `mctl.workflow.id` | `RunActivity:*` | catalog (reserved) | `activity.info().workflow_id` |
| `mctl.workflow.run_id` | `RunActivity:*` | catalog (reserved) | `activity.info().workflow_run_id` |
| `mctl.workflow.type` | `argo.workflow`, investigator pod root | catalog (reserved) | operation minus `mctl-agents-` (`investigate`, `implement`, `approve`, `shepherd`) |
| `mctl.execution.id` | `argo.workflow`, pod root | catalog (reserved) | the `execution_id` operation param; in the investigator pod, the store's `we_` id once resolved |
| `mctl.work_item.id` | `argo.workflow`, pod root | catalog (reserved) | the `work_item_id` param / `--work-item-id` |
| `mctl.argo.workflow.name` | `argo.workflow`, pod root | catalog (reserved) | mctl-api's submit response; `ARGO_WORKFLOW_NAME` in the pod |
| `mctl.argo.workflow.phase` | `argo.workflow` | **proposed** | the terminal Argo phase |
| `mctl.agent.name` | pod root, `invoke_agent` | catalog (reserved) | `issue-investigator` / `implementer` |
| `mctl.repository.name` | `argo.workflow`, pod root, command spans | catalog (reserved) | `owner/repo` only, never a URL |
| `mctl.issue.number` / `mctl.pr.number` | as above | catalog (reserved) | parsed from a github.com URL argument |
| `k8s.pod.name` | resource | catalog (Collector-set) | `HOSTNAME` inside Kubernetes |
| `gen_ai.operation.name` | model/tool spans | catalog (reserved) | `invoke_agent`, `chat`, `execute_tool` |
| `gen_ai.provider.name` | model spans | catalog (reserved) | `anthropic` |
| `gen_ai.request.model` | model spans | catalog (reserved) | the options' model |
| `gen_ai.response.model` | `chat` | upstream | `AssistantMessage.model` |
| `gen_ai.agent.name` | `invoke_agent` | upstream | the agent name |
| `gen_ai.usage.input_tokens` / `output_tokens` | `chat` (per message), `invoke_agent` (summed over result frames) | catalog (reserved) | SDK `usage` |
| `gen_ai.tool.name` / `mctl.tool.name` | `execute_tool` | upstream / catalog (shipped) | `ToolUseBlock.name` |
| `mctl.tool.status` | `execute_tool` | catalog (shipped) | `ok`, `error`, `incomplete` |
| `mcp.method.name` | `execute_tool mcp__*` | upstream | `tools/call` |
| `mctl.github.operation` | command spans | **proposed** | e.g. `issue.comment`, `pr.create`, `api.write`, `push`, `commit` |
| `mctl.github.mutation` | command spans | **proposed** | `true` when the operation writes to GitHub |
| `mctl.artifact.name` / `mctl.artifact.kind` | `mctl.artifact.write` event | **proposed** | a file name (`requirements.md`) and `proposal` |
| `mctl.policy.rule_id`, `.decision`, `.code`, `.version`, `.action_kind`, `.operation` | `mctl.policy.decision` event | **proposed** | `orchestrator/policy_checkpoint.emit` |
| `error.type` | any failed span | upstream | exception class, `exit_<n>`, `http_<status>`, `tool_error`, or an Argo phase |

Latency is the span's own duration. A `chat` span starts when its input was complete
(the query, the previous tool result, or its own previous message) and ends at the
last block of its message. So it measures model latency, not tool time. Outcome is
the span status plus `error.type`.

Policy decisions made by the MCP `PreToolUse` hook land on the `invoke_agent` span,
because the SDK runs hooks in the client's context. The tool span is not current at
that point.

## Redaction rules

**Never recorded, and with no opt-in:** prompts, completions and assistant text,
tool arguments and tool results, issue and comment bodies, command lines (argv),
commit messages, file contents and paths, stdout/stderr, tokens and secrets, and
the free-text `reason` of a policy decision or the target it acts on.

These rules are enforced in two places:

1. **At the source.** The call sites only ever read a fixed vocabulary.
   `AgentRunObserver` reads model names, message ids, usage counters, tool names,
   tool-use ids and `is_error`, and never `.text`, `.input` or `.content`.
   `classify_command` copies nothing from argv that it has not matched against a
   closed pattern: the subcommand, `--repo owner/repo`, and a github.com
   issue/PR URL. Every attribute set through a `SpanHandle` is also filtered
   through the guard below.
2. **At export** (`GuardedExporter`), for every span the process emits,
   including `temporalio`'s. Its error status would otherwise carry
   `f"{type(exc).__name__}: {exc}"`. The guard has four parts:
   - **Key allowlist.** `mctl.*`; the `gen_ai.*` operation, provider, model,
     agent, tool and the two usage counters; `mcp.method.name`; `error.type`;
     `exception.type`/`escaped`; `k8s.pod.name`; and temporalio's id keys.
     Everything else is dropped, including any key a future library invents.
   - **Key denylist, on top of the allowlist.** Anything containing
     `authorization`, `cookie`, `api_key`, `secret`, `password`, `credential`,
     `private_key` or `token` (except the two integer usage counters). Anything
     ending in `prompt`, `completion`, `messages`, `arguments`, `args`, `input`,
     `output`, `result`, `body`, `content`, `text`, `payload`, `stdout`,
     `stderr`, `command`, `diff`, `query`, `description` or `comment`. So
     `mctl.issue.body` is refused even though it is in `mctl.*`.
   - **Value check.** Credential shapes (GitHub `gh*_`/`github_pat_`, `sk-`,
     Vault `hv*.`, JWTs, PEM keys, `Bearer …`, `user:pass@` URLs) and any string
     longer than 256 characters are dropped. They are never truncated, because a
     truncated payload is still a payload.
   - **Status and name.** An error status keeps its code and loses its
     description. A span name that fails the credential or shape check is
     replaced with `redacted`.

Values are **dropped, never masked**. A masked value is a present attribute that
reads like data. That is the Collector's `****` problem, mctl-gitops#1332, which
today also masks `gen_ai.usage.*_tokens`. An absent attribute is honest about
what it is.

**The one opt-in:** `MCTL_TRACE_ERROR_DETAIL=true` keeps `exception.message` /
`exception.stacktrace` and error-status descriptions. The value check above still
applies to them. It is off by default and intended for a debugging session, not a
standing deployment.

## Failure isolation

- **Unconfigured means inert.** With no `OTEL_EXPORTER_OTLP_(TRACES_)ENDPOINT`, no
  exporter is built, no span is recorded, and no `opentelemetry` module is even
  imported. `tests/test_tracing.py` checks the last point in a fresh process.
- **Never raises.** Every helper catches its own errors. A span that cannot start
  gives a no-op handle. The traced block's own exceptions propagate unchanged,
  with only their type recorded.
- **Bounded and non-blocking.** A batch processor with a 2048-span queue that
  drops when full, 256-span batches, and a 5 s per-request export timeout (an
  explicit `OTEL_EXPORTER_OTLP_TIMEOUT` still wins). At process exit the flush
  runs on a daemon thread joined for at most 5 s, so a pod never sits in
  `Terminating` behind a dead Collector.
- **Logged once.** Each failure kind is logged once per process, and the SDK's
  own exporter loggers are capped at one line.

## Environment variables

| Variable | Where | Default | Effect |
|---|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` or `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | worker, pods | unset | Turns tracing on. Standard OTLP semantics (`/v1/traces` is appended to the generic one). |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | worker, pods | `http/protobuf` | Only `http/protobuf` is shipped (mctl-agent#38 found gRPC-only failing silently). Any other value keeps tracing off and logs once. |
| `OTEL_SERVICE_NAME` | worker, pods | `mctl-agents-worker` / `-investigator` / `-implementer` | `service.name` |
| `OTEL_RESOURCE_ATTRIBUTES`, `OTEL_EXPORTER_OTLP_HEADERS`, `OTEL_EXPORTER_OTLP_TIMEOUT` | worker, pods | standard | Standard SDK behaviour |
| `OTEL_SDK_DISABLED=true` / `OTEL_TRACES_EXPORTER=none` | worker, pods | unset | Forces tracing off. |
| `MCTL_TRACE_ARGO_PARAM` | worker | off | Sends `traceparent` as an operation parameter. Turn it on only after the CWFT and the mctl-api registry declare it. |
| `TRACEPARENT` | pods | unset | W3C parent of the pod's root span. A malformed or empty value starts a fresh trace. |
| `ARGO_WORKFLOW_NAME` | pods | unset | Stamped as `mctl.argo.workflow.name` on the pod root. |
| `MCTL_TRACE_ERROR_DETAIL` | worker, pods | off | The one redaction opt-in (see above). |

## Follow-ups outside this repo

The rollout order is fail-closed: gitops first, then mctl-api, then the flag in this
repo. mctl-api **drops** an undeclared operation parameter with a warning today
(`StripUndeclared` in `internal/api/handlers_write.go`) and intends to tighten
that to a rejection.

### mctl-gitops (not changed by this PR)

1. **CWFT parameter and pod env.**
   - Apply to `cwft-mctl-agents-investigate.yaml` (template `run-investigator`)
     and `cwft-mctl-agents-implement.yaml` (template `run-implementer`).
     Optionally also approve and shepherd.
   - Declare the parameter under `spec.arguments.parameters`:

     ```yaml
     - name: traceparent
       value: ""
       # W3C trace context of the Temporal activity that submitted this run
       # (mctl-agents#195). Empty = the pod starts its own trace.
     ```

   - Add to the agent container's `env:`:

     ```yaml
     - name: TRACEPARENT
       value: "{{workflow.parameters.traceparent}}"
     - name: ARGO_WORKFLOW_NAME
       value: "{{workflow.name}}"
     - name: OTEL_EXPORTER_OTLP_ENDPOINT
       value: http://otel-collector.monitoring.svc.cluster.local:4318
     - name: OTEL_EXPORTER_OTLP_PROTOCOL
       value: http/protobuf
     ```

   - Env only, never interpolated into the script body.
     `scripts/validate-shell-param-interpolation.py` enforces that.
   - The two OTLP variables can wait until step 4. Without them the pod is inert.
2. **Worker Deployments.**
   - Set `otel.enabled: true` in `services/admins/mctl-agents-worker/values.yaml`,
     `mctl-agents-worker-exec/values.yaml` and
     `mctl-agents-worker-implement/values.yaml`. The base-service chart then
     renders the endpoint, `http/protobuf`, `OTEL_SERVICE_NAME` and
     `OTEL_RESOURCE_ATTRIBUTES`.
   - After steps 1 and 3 have shipped, also set `env.MCTL_TRACE_ARGO_PARAM: "true"`
     on the worker(s) that run `submit_and_wait`: exec and implement, plus
     `mctl-agents-worker` while it keeps `submit_and_wait` registered.
3. **Network.** Confirm the Argo agent pods' namespace may egress to
   `otel-collector.monitoring:4318`.
4. **Collector.**
   - Configure a trace backend (mctl-gitops#1280).
   - Fix the `.*token.*` redaction pattern that masks `gen_ai.usage.*_tokens`
     (mctl-gitops#1332).

### mctl-api

- Declare an optional parameter on `mctl-agents-investigate` and
  `mctl-agents-implement` (and approve/shepherd if their CWFTs take it):
  `{Name: "traceparent", Type: "string", Required: false, Pattern:
  "^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"}`.
- Ship it after the CWFT change, because Argo rejects parameters a template does
  not declare.

### mctl-docs

- Reserve the **proposed** names above in `docs/reference/telemetry-attributes.md`,
  in their own PR as the catalog requires.
- Promote the reserved names this code now emits to `shipped`, naming
  `orchestrator/tracing.py` and `orchestrator/temporal/activities/argo.py`.

## Live-evidence checklist for closing #195

Everything above is offline evidence. #195's acceptance criteria are about a real
run, so it closes on this list, each item with a link or a screenshot:

1. [ ] **Backend chosen and wired.** mctl-gitops#1280 is decided, and the
   Collector's `backendEndpoint` points at it. A synthetic span sent to
   `otel-collector.monitoring:4318` (e.g. `telemetrygen traces
   --otlp-http --otlp-insecure`) is visible in the backend.
2. [ ] **Token counters survive the Collector** (mctl-gitops#1332 closed).
   `gen_ai.usage.input_tokens` arrives as an integer, not `****`.
3. [ ] **Catalog updated.** The mctl-docs reservation PR for the proposed names
   is merged.
4. [ ] **Worker env.** `otel.enabled: true` is live on all three worker
   Deployments. `kubectl exec` shows `OTEL_EXPORTER_OTLP_ENDPOINT` and
   `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`. The worker log says
   `execution tracing enabled`.
5. [ ] **CWFT wiring.** Both CWFTs declare `traceparent` and map it to
   `TRACEPARENT`, set `ARGO_WORKFLOW_NAME` and the OTLP env. The rendered
   template (`argo template get` / `kubectl get cwft -o yaml`) shows them.
6. [ ] **mctl-api declares `traceparent`**, released and deployed. Submitting
   with the parameter logs no `ignoring undeclared operation parameters` line.
7. [ ] **Flag on.** `MCTL_TRACE_ARGO_PARAM=true` on the worker(s) running
   `submit_and_wait`.
8. [ ] **One real investigation, end to end.** Label a real issue
   `agents:intake`, and in the backend find the trace by `mctl.workflow.id`
   (`dev-loop-<owner>-<repo>-<n>`). It must show, in one tree:
   `RunActivity:submit_and_wait` → `argo.workflow mctl-agents-investigate`
   → `issue-investigator.run` → `invoke_agent` with `chat` spans carrying token
   usage and `execute_tool` spans with status → `github.issue.comment`
   (`mctl.github.mutation=true`) → `mctl.artifact.write` events for the triplet.
9. [ ] **Correlation holds.** On that trace:
   - `mctl.argo.workflow.name` equals the Argo workflow the mctl-api execution
     record names.
   - `k8s.pod.name` equals the pod in `argo get`.
   - `mctl.workflow.run_id` equals `temporal workflow describe`.
   - With the work-item layer on, `mctl.execution.id` is the `we_` id in
     `work_context`.
10. [ ] **One real implementation** in the same trace family: `implementer.run`
    with `git.commit`, `git.push` and `github.pr.create`
    (`mctl.repository.name`, `mctl.github.mutation=true`).
11. [ ] **Redaction holds on real data.** Export that trace as JSON and grep it
    for the issue's body text, a line of the proposal, `ghp_`/`ghs_`/`sk-`,
    `Bearer` and the prompt's opening sentence. Expect zero hits.
12. [ ] **Failure isolation holds live.** Scale the Collector to zero (or point
    one worker at a dead endpoint) for one investigation. The run still
    succeeds, the worker and pod each log one `tracing:` warning, and the pod
    exits within the 5 s flush bound.
