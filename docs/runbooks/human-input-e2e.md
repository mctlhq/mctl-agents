# Live proof: investigator -> human -> automatic continuation

Evidence run for mctlhq/mctl-agents#473 (epic mctlhq/.github#42). The automated
equivalent is `tests/test_human_input_e2e.py`.

## Prerequisites

- mctl-gitops: `mctl-agents-investigate` declares `human_input_responses` and passes it
  as `--human-input-responses`.
- mctl-api: `human_input_responses` is on the parameter allow-list (mctl-api#372 strips
  undeclared parameters).
- The `issue-investigator-default` ExecutionProfile grants `human.request_input`, and
  `ISSUE_INVESTIGATOR_RESOLVER_MODE=declarative`.
- mctl-telegram#571 (Telegram adapter), or use `temporal workflow signal` below.

Without the CWFT parameter a continuation re-asks the question; the workflow skips it by
`question_hash` and proceeds to approval, so the failure is benign.

## Steps

1. Open a deliberately ambiguous test issue labelled `agents:intake`, in a service repo
   whose author you control (the issue author is the only authorized respondent).
2. Run `mctl_trigger_issue` with `use_temporal=true`.
3. Watch `human_input_state` (Temporal query) until `WAITING_FOR_INPUT`; note `request_id`.
4. Answer through Telegram, or signal `human_input_response` with a
   `HumanInputResponse` payload (`respondent` = `github:<issue author>`).
5. Verify:
   - `human_input_state.state` returns to `RUNNING` and a second
     `mctl-agents-investigate` run starts with `human_input_responses` carrying that
     `request_id` and `request_hash`.
   - The published proposal has `human-input/answered.json` (ids, hashes, timestamps;
     no answer text) and no answered `request.json`.
   - The loop parks waiting for approval and the implementer has not run.
   - After `approve`, the implementer runs and `result.human_input.outcome == "answered"`.

## Evidence template for #473

```
workflow_id:      <id>          run_id: <id>
request_id:       hir-...       request_hash: sha256:...
answered via:     telegram | signal
continuation:     <argo workflow name>
answered.json:    <gitops commit URL>
approval park:    implementer not run before approve: yes/no
```
