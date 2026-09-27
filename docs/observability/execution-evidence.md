# Execution evidence

The auditable record of what a governed execution did (mctlhq/mctl-agents#199,
ADR 015: `docs/adr/015-execution-evidence-contract.md`): one immutable,
versioned, content-addressed `ExecutionEvidence` document per execution,
joining execution identity (#196), policy decisions (#197), durable
approvals (#198) and produced artifacts into one place that answers "what
happened, under which policy, who approved it, and what did it produce" —
without depending on execution traces (#195) existing or being exported.

- Code: `orchestrator/execution_evidence.py` (the stdlib-only contract module
  and the in-process `EvidenceRecorder`), `orchestrator/evidence_store.py`
  (Tier A persistence, retrieval, and the `show` CLI).
- Tests: `tests/test_execution_evidence.py`, `tests/test_evidence_store.py`.
- Related: `docs/observability/execution-traces.md` (#195 — the "what is
  happening" view; `trace_id` is the join key between the two documents, not
  a dependency of either on the other).

## Status

The contract module, the recorder, the completeness checker, and the Tier A
gitops-backed store and CLI are built and tested. **Wiring the recorder into
production call sites is not done yet** — `policy_checkpoint._emit`,
`tracing.record_artifact`'s call sites, `AgentRunObserver`, and the run
entrypoints (`run_issue_investigator.py`, `run_implementer.py`,
`run_shepherd.py`) do not currently call `EvidenceRecorder.offer_*` or
`orchestrator.execution_evidence.set_recorder`. Until that wiring lands, no
document is sealed or persisted by a real run: the module is inert by
construction (the recorder sink is unset by default, so an unwired call site
costs nothing and changes no existing behaviour). See ADR 015's
"Follow-ups" section.

## Shape

```text
ExecutionEvidence  evidence.mctl.ai/v1alpha1
├── evidence_id            "ev-" + content_hash[7:23]
├── content_hash           sha256: over every field except itself, evidence_id, created_at
├── created_at
├── execution               trace_id, context_id, workflow ids, attempt, timing
├── identity                actor, executor, environment, tenant, repository, context_trust
├── models[]                (provider, model) -> turns, input/output tokens, usage_record_ref
├── actions[]                one per policy_checkpoint.Decision
├── approvals[]              one per resolved mctl-api approval receipt
├── artifacts[]              proposal files, branch, PR, merge commit — digests, never contents
├── evaluations[]            policy_compliance, evidence_completeness, ... (#60)
├── completeness             status + gaps[] (the issue's "detect incomplete evidence")
├── outcome                  status + code, no free-text message
└── retention                class (ADR 009's vocabulary) + expires_after_days
```

No field is a payload: no prompt, completion, tool argument or result, issue
or comment body, argv, commit message, file content, or credential ever
reaches a sealed document. `execution_evidence._safe()` mirrors
`tracing_sdk.GuardedExporter`'s redaction rule (drop, never mask or
truncate, a string over 256 characters or matching a credential shape); a
`target` is stored as a bounded `target_ref` only when it matches a closed,
already-public allowlist, and as a `target_digest` otherwise.

## Completeness

`check_completeness()` flags the issue's central ask — a governed mutation
with no policy decision behind it — as a gap, never as a silent pass:

| Gap code | Meaning |
|---|---|
| `mutation_without_decision` | a mutating-kind artifact exists but no permitted, mutation-classified action backs it |
| `approval_unresolved` | an approved action's receipt could not be resolved |
| `decision_without_outcome` | an approved action's receipt was resolved but never shows as consumed |
| `identity_unavailable` | no control-plane execution context was loadable |
| `ungoverned_transport` | standing gap for a builder that grants `Bash` (ADR 014 open decision 4) — supplied by the caller, never inferred |

`completeness.status` is `COMPLETE` iff `gaps` is empty. The built-in
`policy_compliance` evaluation is `PASS` only when every recorded mutation
is permitted **and** completeness is `COMPLETE`.

## Storage and retrieval (Tier A)

```text
platform-gitops/agents-state/_evidence/
  <workflow_type>/<temporal_workflow_id>/<attempt>-<evidence_id>.json
  by-trace/<trace_id>/<evidence_id>            # pointer file: the record path
```

```bash
python -m orchestrator.evidence_store show --workflow-id dev-loop-mctlhq-mctl-agents-199
python -m orchestrator.evidence_store show --trace-id <32-hex-trace-id>
python -m orchestrator.evidence_store show --evidence-id ev-...
```

The command prints the canonical JSON of the sealed document unchanged — an
exported document's bytes, reloaded and rehashed, reproduce its own
`content_hash`. A document that does not (`is_trustworthy()` returns
`False`) is reported as `UNTRUSTED` on stderr and is never printed as
evidence.

## Tier B (follow-up, not built here)

An mctl-api evidence store — `POST .../evidence` /
`GET /api/v1/evidence/{evidence_id}` — shaped byte-for-byte on
`orchestrator/work_context/snapshots.py`'s `canonical_b64` + `content_hash`
upload pattern, so that store is an additive import over the same sealed
bytes, not a schema change. `ContextSnapshot.evidence_refs` dereferences
through whichever tier is configured once it exists. Tracked as a follow-up
issue to mctlhq/mctl-agents#199; not opened as part of this change.
