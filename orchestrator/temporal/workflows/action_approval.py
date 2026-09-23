"""The durable wait for a human approval (mctlhq/mctl-agents#198,
docs/adr/014-policy-checkpoint.md §7).

mctl-api is the approval authority (mctl-api#366/#367). Temporal is only the
wait/resume mechanism, and nothing in this module's state ever authorizes a
side effect: every wake re-runs the gated activity, which recomputes the
intent and redeems the receipt through the policy checkpoint.

## Shape

    caller workflow ── run_gated_action(activity, GatedActionInput)
        │  1. execute_activity(activity)        -> GatedActionResult
        │     ran / refused ........................ return, no wait
        │     approval_pending (receipt aar_X) ..... 2.
        └─ 2. execute_child_workflow(ActionApprovalWaitWorkflow,
                                     id="action-approval-aar_X")
               WAITING_FOR_APPROVAL, holding no pod and no activity:
                 wait_condition(signal) bounded by a durable timer that
                 fires every `poll_seconds` until the receipt's expiry
               on a signal ........ re-run the activity with approval_ref
               on a timer tick .... read_action_approval (read-only GET);
                                    re-run only if the store says decided
               outcome: ran | denied | expired | consumed | mismatch |
                        refused | blocked | timed_out

## Why a child workflow keyed by the receipt

- **Addressable by what mctl-api already stores.** The wake-up signal is
  sent to `action-approval-<approval id>`. mctl-api knows the id of every
  request it decides, and nothing else: it does not need to know which
  DevLoop, which step or which task queue asked. The signal contract is
  therefore one id and one name (`action_approval_decided`), and no field
  has to be added to ActionApprovalRequest.
- **One place for the wait.** The step that can hit REQUIRE_APPROVAL
  changes as policy changes (today only an agent's own `mcp__mctl__*` call
  inside a pod does, and that is not a Temporal activity at all). Any
  workflow step that moves its side effect into a gated activity gets the
  same wait from one helper, instead of each workflow growing its own
  signal handler, timer and poll.
- **No new commands on an existing workflow.** A brand-new workflow type
  has no history to disagree with. The caller adds commands only when it
  starts using `run_gated_action`, and guards that with its own
  `workflow.patched()` marker at that point.

## Outcomes

- `ran`: the side effect ran exactly once, after the consume.
- `consumed`: the receipt was already spent (a retry after a crash between
  the consume and the side effect, or a second waiter). It is never re-run.
- `denied`, `expired`, `timed_out`: a human said no, or nobody answered in
  time. Asking again is a deliberate caller decision: `next_attempt()`
  makes a new request under a new `attempt`, which needs a new decision.
- `mismatch`: the action the activity would perform now is not the one
  that was approved. Nothing is consumed.
- `refused`: the store refused the request itself.
- `blocked`: the policy refused without an approval flow (DENY, or the
  approval store is off: `approval_required`).
- `undecided`: from the caller's first call only, the checkpoint could not
  decide (an unreachable store). Inside the wait that is retried with
  backoff while the timer runs, never a terminal outcome.

A policy decision is never inherited: every wake decides again in a fresh
activity, and every attempt is its own request with its own receipt.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

with workflow.unsafe.imports_passed_through():
    from orchestrator import policy_checkpoint as pc
    from orchestrator.temporal.activities.action_approval import (
        ApprovalPoll,
        GatedActionInput,
        GatedActionResult,
        read_action_approval,
    )

#: The child's workflow id is this plus the receipt id, and it is the
#: address mctl-api signals after recording a decision.
WORKFLOW_ID_PREFIX = "action-approval-"
#: The wake-up signal. Payload: `{"approval_id": "aar_..."}` (or the bare
#: id). A wake-up only: it is never trusted as an approval.
DECIDED_SIGNAL = "action_approval_decided"
STATE_QUERY = "action_approval_state"

#: ADR-014 §7: poll every 15 minutes while no signal arrives.
DEFAULT_POLL_SECONDS = 15 * 60
#: A hard ceiling on the wait whatever the receipt says: mctl-api caps a
#: request's expiry at 7 days, and an unreadable expiry must not wait forever.
DEFAULT_MAX_WAIT_SECONDS = 7 * 24 * 3600
MIN_POLL_SECONDS = 1
#: Re-checks the store could not answer back off from this up to the poll.
RECHECK_BACKOFF_SECONDS = 30

GATED_ACTIVITY_TIMEOUT = timedelta(minutes=10)
#: A retry re-decides from scratch; after a consume it answers
#: `approval_consumed`, so a retry can never run the side effect twice.
GATED_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=3)
POLL_TIMEOUT = timedelta(seconds=60)
POLL_RETRY_POLICY = RetryPolicy(maximum_attempts=3)

WAITING_FOR_APPROVAL = "WAITING_FOR_APPROVAL"
RECHECKING = "RECHECKING"
DONE = "DONE"

OUTCOME_RAN = "ran"
OUTCOME_DENIED = "denied"
OUTCOME_EXPIRED = "expired"
OUTCOME_TIMED_OUT = "timed_out"
OUTCOME_CONSUMED = "consumed"
OUTCOME_MISMATCH = "mismatch"
OUTCOME_REFUSED = "refused"
OUTCOME_BLOCKED = "blocked"
OUTCOME_UNDECIDED = "undecided"
#: Another wait on the same receipt is already running.
OUTCOME_ALREADY_WAITING = "already_waiting"

#: The outcomes after which asking again is legitimate. Never after `ran`
#: or `consumed`: that action has happened (or burned its receipt).
REREQUESTABLE = frozenset({OUTCOME_DENIED, OUTCOME_EXPIRED, OUTCOME_TIMED_OUT})

#: Store states that mean "a decision exists": worth a re-check.
_DECIDED_STATES = frozenset({"approved", "denied", "expired", "consumed"})

_TERMINAL_CODES = {
    pc.CODE_APPROVAL_DENIED: OUTCOME_DENIED,
    pc.CODE_APPROVAL_EXPIRED: OUTCOME_EXPIRED,
    pc.CODE_APPROVAL_CONSUMED: OUTCOME_CONSUMED,
    pc.CODE_APPROVAL_INTENT_MISMATCH: OUTCOME_MISMATCH,
    pc.CODE_APPROVAL_REFUSED: OUTCOME_REFUSED,
}
_KEEP_WAITING = frozenset({pc.CODE_APPROVAL_PENDING, *pc.UNDECIDED_CODES})


def approval_workflow_id(approval_id: str) -> str:
    return f"{WORKFLOW_ID_PREFIX}{approval_id}"


def outcome_of(result: GatedActionResult) -> str | None:
    """The outcome one gated call ends the step with, or None to keep
    waiting. Only a call that actually ran is `ran`."""
    if result.ran:
        return OUTCOME_RAN
    if result.code in _KEEP_WAITING:
        return None
    return _TERMINAL_CODES.get(result.code, OUTCOME_BLOCKED)


@dataclass(frozen=True)
class ApprovalWaitInput:
    approval_id: str
    #: The registered name of the gated activity to re-run on a wake.
    activity: str
    action: GatedActionInput
    poll_seconds: int = DEFAULT_POLL_SECONDS
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS
    activity_timeout_seconds: int = int(GATED_ACTIVITY_TIMEOUT.total_seconds())
    #: Empty: the workflow's own task queue.
    activity_task_queue: str = ""


@dataclass(frozen=True)
class ApprovalWaitResult:
    outcome: str
    approval_id: str = ""
    attempt: int = 0
    #: The last checkpoint code seen.
    code: str = ""
    result: dict[str, Any] | None = None
    reason: str = ""
    signal_wakes: int = 0
    poll_wakes: int = 0
    rechecks: int = 0

    @property
    def ran(self) -> bool:
        return self.outcome == OUTCOME_RAN


@dataclass(frozen=True)
class ApprovalWaitState:
    state: str
    approval_id: str
    attempt: int = 0
    deadline: str = ""
    last_poll_state: str = ""
    last_code: str = ""
    signal_wakes: int = 0
    ignored_signals: int = 0
    poll_wakes: int = 0
    rechecks: int = 0


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _signal_ref(args: tuple[object, ...]) -> str:
    """The receipt id a signal names, or "" when it names none. Untrusted:
    only ever compared with the id this wait was started for."""
    if len(args) != 1:
        return ""
    raw = args[0]
    if isinstance(raw, dict):
        raw = raw.get("approval_id")
    return raw if isinstance(raw, str) else ""


def _activity_options(*, timeout_s: int, task_queue: str) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "result_type": GatedActionResult,
        "start_to_close_timeout": timedelta(seconds=max(1, timeout_s)),
        "retry_policy": GATED_ACTIVITY_RETRY_POLICY,
    }
    if task_queue:
        opts["task_queue"] = task_queue
    return opts


@workflow.defn
class ActionApprovalWaitWorkflow:
    """Waits on one ActionApprovalRequest, then re-runs its gated activity.
    Started by `run_gated_action` under `approval_workflow_id(<receipt>)`."""

    @workflow.init
    def __init__(self, inp: ApprovalWaitInput) -> None:
        self._approval_id = inp.approval_id
        self._attempt = inp.action.attempt
        self._state = WAITING_FOR_APPROVAL
        self._wake = False
        self._deadline = ""
        self._last_poll_state = ""
        self._last_code = pc.CODE_APPROVAL_PENDING
        self._signal_wakes = 0
        self._ignored_signals = 0
        self._poll_wakes = 0
        self._rechecks = 0

    @workflow.signal(name=DECIDED_SIGNAL)
    def decided(self, *args: object) -> None:
        """mctl-api's best-effort wake-up after it recorded a decision.
        Several signals before the wait wakes collapse into one re-check;
        a signal naming another receipt is ignored."""
        if _signal_ref(args) != self._approval_id or self._state == DONE:
            self._ignored_signals += 1
            return
        self._signal_wakes += 1
        self._wake = True

    @workflow.query(name=STATE_QUERY)
    def state(self) -> ApprovalWaitState:
        return ApprovalWaitState(
            state=self._state, approval_id=self._approval_id, attempt=self._attempt, deadline=self._deadline,
            last_poll_state=self._last_poll_state, last_code=self._last_code,
            signal_wakes=self._signal_wakes, ignored_signals=self._ignored_signals,
            poll_wakes=self._poll_wakes, rechecks=self._rechecks,
        )

    async def _poll(self) -> ApprovalPoll:
        poll = await workflow.execute_activity(
            read_action_approval, self._approval_id,
            start_to_close_timeout=POLL_TIMEOUT, retry_policy=POLL_RETRY_POLICY,
        )
        self._last_poll_state = poll.state
        return poll

    async def _recheck(self, inp: ApprovalWaitInput) -> GatedActionResult:
        self._state = RECHECKING
        self._rechecks += 1
        # The receipt this wait was started for, and nothing the workflow
        # believes about it: the activity redeems it through the checkpoint.
        action = replace(inp.action, approval_ref=inp.approval_id)
        result: GatedActionResult = await workflow.execute_activity(
            inp.activity, action,
            **_activity_options(timeout_s=inp.activity_timeout_seconds, task_queue=inp.activity_task_queue),
        )
        self._last_code = result.code
        self._state = WAITING_FOR_APPROVAL
        return result

    def _done(self, inp: ApprovalWaitInput, outcome: str, result: GatedActionResult | None) -> ApprovalWaitResult:
        self._state = DONE
        workflow.logger.info(
            "action_approval.done",
            extra={"approval_id": inp.approval_id, "outcome": outcome, "code": self._last_code,
                   "attempt": self._attempt},
        )
        return ApprovalWaitResult(
            outcome=outcome, approval_id=inp.approval_id, attempt=self._attempt, code=self._last_code,
            result=result.result if result is not None and result.ran else None,
            reason=result.reason if result is not None else "",
            signal_wakes=self._signal_wakes, poll_wakes=self._poll_wakes, rechecks=self._rechecks,
        )

    @workflow.run
    async def run(self, inp: ApprovalWaitInput) -> ApprovalWaitResult:
        poll_s = max(MIN_POLL_SECONDS, inp.poll_seconds)
        deadline = workflow.now() + timedelta(seconds=max(MIN_POLL_SECONDS, inp.max_wait_seconds))
        first = await self._poll()
        expires = _parse_time(first.expires_at)
        if expires is not None and expires < deadline:
            deadline = expires
        self._deadline = deadline.isoformat()
        workflow.logger.info(
            "action_approval.wait_started",
            extra={"approval_id": inp.approval_id, "attempt": self._attempt, "deadline": self._deadline},
        )

        recheck = first.state in _DECIDED_STATES
        retry_in: float | None = None
        backoff = float(RECHECK_BACKOFF_SECONDS)
        while True:
            if recheck or self._wake:
                self._wake = False
                recheck = False
                result = await self._recheck(inp)
                outcome = outcome_of(result)
                if outcome is not None:
                    return self._done(inp, outcome, result)
                if result.code in pc.UNDECIDED_CODES:
                    # The store could not answer: re-check again soon, with
                    # backoff, while the timer still runs.
                    retry_in = min(backoff, float(poll_s))
                    backoff = min(backoff * 2, float(poll_s))
                else:
                    retry_in = None
                    backoff = float(RECHECK_BACKOFF_SECONDS)
            remaining = (deadline - workflow.now()).total_seconds()
            if remaining <= 0:
                # One last read-only look, so a decision that landed after
                # the last tick (and whose signal was lost) is not dropped.
                last = await self._poll()
                if last.state == "approved":
                    result = await self._recheck(inp)
                    outcome = outcome_of(result)
                    if outcome is not None:
                        return self._done(inp, outcome, result)
                return self._done(inp, OUTCOME_TIMED_OUT, None)
            timeout = min(float(poll_s), remaining, retry_in if retry_in is not None else float(poll_s))
            try:
                await workflow.wait_condition(lambda: self._wake, timeout=timedelta(seconds=timeout))
                continue  # a signal: re-check at the top of the loop
            except TimeoutError:
                pass
            if retry_in is not None:
                recheck = True
                continue
            self._poll_wakes += 1
            poll = await self._poll()
            recheck = poll.state in _DECIDED_STATES


async def run_gated_action(
    activity: str,
    action: GatedActionInput,
    *,
    poll_seconds: int = DEFAULT_POLL_SECONDS,
    max_wait_seconds: int = DEFAULT_MAX_WAIT_SECONDS,
    activity_timeout: timedelta = GATED_ACTIVITY_TIMEOUT,
    activity_task_queue: str = "",
) -> ApprovalWaitResult:
    """Run the gated activity `activity` once; if its checkpoint answers
    `approval_pending`, wait durably for the human in a child workflow keyed
    by the receipt, and re-run it on every wake. Call from workflow code.

    A caller adding this to an existing workflow adds commands, so it must
    guard the call with its own `workflow.patched()` marker."""
    first: GatedActionResult = await workflow.execute_activity(
        activity, replace(action, approval_ref=""),
        **_activity_options(timeout_s=int(activity_timeout.total_seconds()), task_queue=activity_task_queue),
    )
    if not first.awaiting_approval:
        outcome = outcome_of(first) or OUTCOME_UNDECIDED
        return ApprovalWaitResult(
            outcome=outcome, approval_id=first.approval_ref, attempt=action.attempt, code=first.code,
            result=first.result if first.ran else None, reason=first.reason,
        )
    wait = ApprovalWaitInput(
        approval_id=first.approval_ref, activity=activity, action=replace(action, approval_ref=""),
        poll_seconds=poll_seconds, max_wait_seconds=max_wait_seconds,
        activity_timeout_seconds=int(activity_timeout.total_seconds()), activity_task_queue=activity_task_queue,
    )
    try:
        return await workflow.execute_child_workflow(
            ActionApprovalWaitWorkflow.run, wait,
            id=approval_workflow_id(first.approval_ref),
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
        )
    except WorkflowAlreadyStartedError:
        return ApprovalWaitResult(
            outcome=OUTCOME_ALREADY_WAITING, approval_id=first.approval_ref, attempt=action.attempt,
            code=first.code,
        )


def next_attempt(previous: ApprovalWaitResult, action: GatedActionInput) -> GatedActionInput:
    """The input for a deliberate re-request after `previous` ended denied,
    expired or timed out: the next `attempt`, and no receipt. The new
    attempt opens a new request that needs a new human decision; nothing
    decided for `previous` carries over."""
    if previous.outcome not in REREQUESTABLE:
        raise ValueError(f"cannot re-request after {previous.outcome!r}; only after {sorted(REREQUESTABLE)}")
    return replace(action, attempt=previous.attempt + 1, approval_ref="")


__all__ = [
    "DECIDED_SIGNAL",
    "STATE_QUERY",
    "ActionApprovalWaitWorkflow",
    "ApprovalWaitInput",
    "ApprovalWaitResult",
    "ApprovalWaitState",
    "approval_workflow_id",
    "next_attempt",
    "outcome_of",
    "run_gated_action",
]
