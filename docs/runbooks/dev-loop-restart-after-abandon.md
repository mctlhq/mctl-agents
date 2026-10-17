# Restarting a DevLoop that was abandoned

Context: mctlhq/mctl-api#640, mctlhq/mctl-agents#636, mctl-gitops#1913.

Each issue has one DevLoop workflow id, `dev-loop-mctlhq-{repo}-{issue}`. A normal start
refuses an id whose latest run completed. That refusal is right for a loop that finished.
It is wrong for a loop an operator ended with `abandon`: the abandon is not a verdict on the
issue. Since mctl-gitops#1913 turned off the legacy approver relay, a running DevLoop is the
only way to implement a proposal.

So a start makes one exception. It starts a new run on the same workflow id when the latest
run:

- closed as **Completed** (abandon makes the workflow return; it never fails, cancels or
  terminates it);
- has a result whose `ended` starts with `abandoned: `
  (`dev_loop.ABANDONED_ENDED_PREFIX`, mirrored by mctl-api's
  `temporalclient.AbandonedEndedPrefix`);
- has a result whose `implement` is null. An abandon during implement or the merge watch
  leaves a PR behind, and a fresh investigate would not resume it.

Every other closed run is still refused. That covers a finished run, a run abandoned after
implement, an expired approval wait, a closed source issue, and Failed, Terminated or
TimedOut. If the result cannot be read, the start fails and nothing is started.

## How to restart

Pick one of these:

- **MCP:** `mctl_trigger_issue(issue_url=..., use_temporal=true)`.
- **REST:** `POST /api/v1/agents/dev-loop/start` with `{"issue_url": ...}`, as an admin.
  - A restart answers 202 with `outcome: restarted_after_abandon`, the new `run_id` and the
    abandoned run as `previous_run_id`. It is audited as `dev-loop-start` with the actor and
    `previous_run_id`.
  - `already_exists` means the latest run was not restartable.
- **CLI:** `python -m orchestrator.temporal.cli start <issue_url>`. It prints
  `restarted <id> after an abandoned run (run_id=...)`, or exits 1 with `not started: ...`.

The roadmap wave route and the `agents:intake` poller never restart. A batch start, or a
label left on an abandoned issue, must not undo an operator's abandon.

## What the new run does

It runs investigate again on the same issue:

- The proposal directory is keyed on the issue number (`run_issue_investigator.resolve_slug`).
  When the old proposal is still `proposed`, the new run rewrites it in place. That is the
  state of a loop abandoned at its approval park.
- If `.status.yaml` is anything else, investigate refuses to overwrite it
  (`_OVERWRITABLE_STATUSES`). So check the status before restarting:

  ```
  gh api repos/mctlhq/mctl-gitops/contents/platform-gitops/agents-state/<repo>/proposals/<slug>/.status.yaml --jq .content | base64 -d
  ```

- The new run gets its own `we_` execution (per-run engine ref) and, because the previous
  one ended, the next `wi_`. Nothing of the abandoned run is reused.
- It parks at approval like any first run. A human approves it
  (`mctl_approve_dev_loop` or `POST /api/v1/agents/dev-loop/{workflow_id}/approve`).

## Pending restarts (from the #636 cutover)

Five pre-1.76.0 loops parked at approval were abandoned on 2026-10-16. mctl-agents#600 was
superseded by #663. Restart these once both halves are released and deployed:

| Issue | Workflow id |
|---|---|
| mctlhq/mctl-agents#413 | `dev-loop-mctlhq-mctl-agents-413` |
| mctlhq/mctl-agents#428 | `dev-loop-mctlhq-mctl-agents-428` |
| mctlhq/mctl-api#489 | `dev-loop-mctlhq-mctl-api-489` |

For each one:

1. Confirm the issue is still open.
2. Confirm its proposal is still `proposed`.
3. Restart it.
4. Check that the response says `restarted_after_abandon`.
