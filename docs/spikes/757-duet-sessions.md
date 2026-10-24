# Spike #757: Duet-style persistent proactive sessions and attention routing

Refs mctlhq/mctl-agents#757. Fixture-only evidence; no runtime change. Prototype and tests:
`docs/spikes/757/`, run with `uv run pytest tests/test_spike_757_attention.py -q`.

## Environment and sources read (default branches, 2026-10-24)

| Repo | SHA | Read |
|---|---|---|
| mctlhq/mctl-agents | cb3c3f99f212ef31dd6822b2707c400fb4687815 | DevLoop, approvals, policy checkpoint |
| mctlhq/mctl-claude-remote | 989bff3b8c0da53cfb109110dfadb022de142e40 | `events/mctl_events/envelope.py`, `events/mctl_events/channel.py` (read in review) |
| mctlhq/.github | 92b1e28327b0d0b575b18007ab6935d45dff057d | `events/schemas/event-envelope.v1.schema.json` |
| mctlhq/mctl-api | b42d63fb7e6e5f488d19df2b3023e9e00ca963f1 | `docs/github-events-producer.md` (subject keys `kind`, `repository`, `number`, `head_sha`; read in review) |
| mctlhq/mctl-telegram | 9472a39cae79455ddd1d58923d8b1033c1c7cc40 | HEAD only; not read |

Not read in this run: mctl-telegram behavior. Claims about it are marked as such. `channel.py` and the
mctl-api producer contract were read during review and confirm the map below.

## Q1. Current-state map

| Primitive | Scope | Where |
|---|---|---|
| DevLoop workflow, approval park, merge watch (15 min poll, `continue_as_new`) | work-item / run | `orchestrator/temporal/workflows/dev_loop.py`, `activities/pr_state.py` |
| Exact-action approvals (`aar_`) | work-item / action | `orchestrator/action_approvals.py` |
| Policy checkpoint (ALLOW/DENY/REQUIRE_APPROVAL) | global / control plane | `orchestrator/policy_checkpoint.py`, ADR 014 |
| WorkItem context | work-item | `orchestrator/work_context/`, ADR 011 |
| Lifecycle ownership (PR phase owner) | entity phase | `orchestrator/lifecycle/`, ADR 010 |
| AgentDefinition / ExecutionProfile / release binding | agent-version | agent registry |
| Inbound events Channel (`mctl.events/v1`, Valkey, deny-by-default routes for `github.pull_request.*`, `github.pull_request_review.submitted`) | session (claude-remote) | mctl-claude-remote, deployed config in mctl-gitops `services/labs/claude-remote/values.yaml` |

## Q2. Is a first-class persistent session needed?

For the PR attention loop: not for the decision itself. The prototype decides from one envelope
plus a re-observed state and a stored digest per watched resource. No transcript or session object
was needed. Other use cases (long-running conversation memory) are #760's scope and were not tested.

## Q3. Event-first vs polling

GitHub PR events already reach the claude-remote session as events (deployed route policy), while the
DevLoop merge watch polls every 15 minutes. Check-run / check-suite types are not routed today, so
CI conclusions are not event-driven. Latency was not measured.

## Q4. Attention routing

Yes: one auditable, deterministic decision separate from authorization. `AttentionDecision v1`
(`docs/spikes/757/attention.py`) with closed `decision` (`IGNORE|CONTINUE|NOTIFY|REQUIRE_APPROVAL`) and
`basis` (`rule|model`). Digest-first IGNORE, first-match YAML rules, a model classifier only for unmatched input,
and unknown state is never "unchanged". Attention, authorization (policy checkpoint) and approval
(`aar_`) stay three separate things; `REQUIRE_APPROVAL` here only means "surface to a human".

## Q5. Persistent context boundary

Surviving state per watched resource is one digest string. Everything else is re-observed through the
system of record (the envelope is reference-only and cannot carry state).

## Q6. Session identity

`session_or_workitem_ref` is an opaque reference (watched resource key or `wi_` id). No session
lifecycle is justified by this evidence.

## Q7. Temporal

Not prototyped (alternative dropped in design). On paper, the existing merge watch with its
`continue_as_new` bounds can host a digest per PR; no second workflow engine is indicated.

## Q8. Actionable notifications

Fixture: two events for one PR in one wake cycle produce one decision (test T4). Delivery is #712's scope.

## Q9. Side-effect boundary

Test T8: `approval_ref` accepts only the `aar_` pattern (equal to `approval_evidence.GRANT_REF_RE`), and
`attention.py` imports no `orchestrator` module and no network module. No approval is created or consumed.

## Q10. API surface

Minimum: the typed decision record, plus a pure `decide()` called by the consumer. No new operator API.

## Measurements

| Measurement | Tag | Result |
|---|---|---|
| Model calls avoided on unchanged state | fixture | 1 of 1 unchanged fixture decided with `basis=rule`, classifier calls 0 |
| Events vs decisions per wake cycle | fixture | 2 events -> 1 decision |
| Duplicate and reordered delivery | fixture | identical decision and `decision_id` |
| Durable state size | fixture | one digest per watched resource |
| Same head, checks pending -> failure | fixture | different digest, `NOTIFY` (`ci_failed`) |
| Event-to-wake latency | not-measured | needs a deployed channel |
| Real-event coverage of rules | not-measured | `observed` is a fixture sidecar |

## D8 ownership map

| Owner | Status | Relation to this spike |
|---|---|---|
| mctl-agents#712 (DevLoop push events) | open | Owns notification delivery; consumes `NOTIFY`/`REQUIRE_APPROVAL`. Not touched. |
| mctl-agents#703 (execution authorization) | closed | Authorization stays there; attention adds none. |
| mctl-agents#197 (policy checkpoints) | closed | `policy_checkpoint` unchanged; verdict names overlap only in spelling. |
| mctlhq/.github#181 / #182 (governance epic / spike) | open / closed | Approval and evidence model reused by pointer only. |
| mctl-agents#746 (portable runtime) | closed | Spike layout convention followed. |
| mctl-agents#760 (per-conversation sessions, coalescing) | open | Owns session store, execution pool and Telegram coalescing; not built here. |

Gap: the re-observation (`ObservedState`) is a fixture. A follow-up must supply it from GitHub MCP tools,
and check events are not routed today.

## Proposed follow-up (not done here)

Wire `decide()` into mctl-claude-remote `channel.py`, filed and reviewed in that repository, re-validating the
copied envelope shape against the producer's tests. The wake-cycle window (time vs delivery batch) is a parameter to settle there.
Also settle there how a refused envelope interacts with the rest of a wake cycle. The prototype refuses the whole
batch (fail closed), which also withholds valid decisions for other resources in it. The wiring should refuse per
resource, or surface the refusal as its own decision, so one malformed delivery cannot hide a valid `NOTIFY` or
`REQUIRE_APPROVAL`.

## Why this verdict

The evidence rules out the two outer outcomes:

- **Not `ADD_AGENT_SESSION_CONTRACT` or `CREATE_PROACTIVE_AGENT_EPIC`.** No persistent session object was needed
  (Q2, Q6). The only state that has to survive a wake is one `observed_state_digest` per watched resource (Q5).
  Everything else is re-observed from the system of record, because the envelope is reference-only. Nothing here
  needs several independently owned slices, so there is no case for a new session contract or an epic.
- **Not `NO_NEW_CONTRACT`.** Composing what exists is not enough. Today the attention outcome is whatever the model
  passes to `ack_event` (`handled | ignored | failed`, `channel.py`): it is untyped, it has no rule or digest
  behind it, and it can be chosen only after the session has already been woken. `AttentionDecision v1` is a
  small new record. It is the part that makes `IGNORE` deterministic and free of model calls, and the part that
  makes coalescing auditable.

That leaves `EXTEND_DEVLOOP_SESSION`, read as **extending the existing durable operator session with attention
state and event wakeups**. It does not mean changing the `DevLoopWorkflow` itself. In concrete terms:

- **The session that gets extended** is the long-lived claude-remote operator session. Event wakeups for it
  already exist through the `mctl.events/v1` Channel, with deny-by-default routes, dedup after ack and
  `max_inflight`.
- **The extension** is a pure `decide()` call in `channel.py` `handle()`, before
  `emit(notification_for(...))`. An `IGNORE` is acked without waking the model. Every other outcome is pushed
  with the `AttentionDecision` attached. The per-resource digest is kept next to the existing dedup keys in Valkey.
- **The DevLoop is untouched.** Its merge watch keeps polling, and #712 still owns DevLoop push events. A DevLoop
  consumer of `AttentionDecision` is possible later, but this spike does not show that one is needed.
- **Authorization and approval are untouched.** `REQUIRE_APPROVAL` only points at an existing `aar_` record.

The outcome name says DevLoop because the issue was framed before the claude-remote Channel shipped. The owner
accepted the verdict with the reading above on 2026-10-24.

## Decision

EXTEND_DEVLOOP_SESSION
