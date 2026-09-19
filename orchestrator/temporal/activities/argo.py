"""Activity: submit an mctl-api operation (which maps 1:1 to an Argo
ClusterWorkflowTemplate, e.g. "mctl-agents-investigate") and poll it to a
terminal status.

Retry ownership (plan phase 4's explicit rule): the CWFTs already retry
*within* a run — on a failed attempt they re-run on a second OAuth account
(claude-code-oauth-token-2), which exists precisely because the first
account hits the five-hour 429. A Temporal RetryPolicy that re-submitted
would multiply real SDK runs (3 attempts x 2 accounts = 6), burning quota
hardest exactly when it's already exhausted. To get resumability without
that multiplication, this activity distinguishes "retry before submission
happened" (must re-submit) from "retry after submission happened" (must
NOT re-submit, only resume polling): the first successful submission is
recorded via `activity.heartbeat(workflow_name)` before the poll loop
starts, and a new attempt reads it back via
`activity.info().heartbeat_details` — if present, submission is skipped
entirely and polling resumes against the same Argo workflow name.
DevLoopWorkflow's retry policy for this activity may therefore allow a
small number of attempts (see SDK_STEP_RETRY_POLICY in workflows/dev_loop.py)
without a retry resubmitting once a workflow_name has actually been
heartbeated — including a response mctl-api returned 2xx for but whose body
couldn't be parsed for a name (see _SUBMITTED_UNKNOWN_NAME below), which
fails loudly on retry rather than guessing. The one gap this doesn't close:
a crash strictly between the Argo POST succeeding and that first heartbeat
call landing at the Temporal server — no I/O happens in between, so it's
vanishingly unlikely, but not impossible.

Do not re-read Argo for a result: mctl-agents-investigate carries
ttlStrategy.secondsAfterCompletion=86400, so a workflow that resumes after
that finds the Argo Workflow gone. This activity returns the terminal
result at completion time; callers must persist whatever they need (see
activities/state.py's record_execution) rather than re-querying Argo later.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from temporalio import activity

from orchestrator.temporal.constants import IMPLEMENTATION_OPERATION
from orchestrator.temporal.implement_outcome import observe_implementer
from orchestrator.temporal.mctl_client import MCTL_API_BASE_URL, auth_headers

REQUEST_TIMEOUT_SECONDS = 30.0
POLL_INTERVAL_SECONDS = 15.0
# Argo Workflow status.phase terminal values. "" / "Pending" / "Running" all
# mean "keep polling" — anything not in this set is treated as non-terminal
# rather than guessed at, since a not-yet-populated status block also reads
# as an empty phase right after submission.
TERMINAL_PHASES = frozenset({"Succeeded", "Failed", "Error"})
# How many consecutive poll failures (mctl-api 5xx, network blip) to ride
# out before giving up. Because this activity runs with maximum_attempts=1
# (see the module docstring), letting any single poll failure propagate
# would kill the whole activity hours into an already-submitted Argo run —
# losing track of it entirely, since the workflow_name isn't persisted
# anywhere else. At POLL_INTERVAL_SECONDS=15s, this rides out ~5 minutes of
# a down mctl-api before finally raising.
MAX_CONSECUTIVE_POLL_ERRORS = 20


@dataclass(frozen=True)
class SubmitAndWaitInput:
    # mctl-api operation name — matches the target ClusterWorkflowTemplate
    # name 1:1 for every mctl-agents-* operation (see
    # internal/operations/registry.go and AgentManifest.cluster_workflow_template).
    operation: str
    params: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkflowResult:
    workflow_name: str
    phase: str  # Succeeded | Failed | Error
    started_at: str | None = None
    finished_at: str | None = None
    # What the implementer itself did, for the implement operation only
    # (#395; see implement_outcome.py). None on every other operation and
    # on results recorded before these fields existed — which the
    # classifier treats as "unknown", never as "did not run".
    implementer_ran: bool | None = None
    implementer_phase: str | None = None
    implementer_started_at: str | None = None
    finalization_phase: str | None = None

    @property
    def succeeded(self) -> bool:
        return self.phase == "Succeeded"


# Runtime phases an implement submit passes through, published as the
# second heartbeat detail so Temporal's describe API can project them
# (mctl-agents#389). This IS the runtime-state surface: Temporal + Argo are
# the durable runtime state, git is the durable lifecycle state, and no
# in-progress marker is pushed to git mid-attempt.
#
#   admitted   a worker slot took the activity (schedule-to-start is over)
#   submitted  Argo accepted the workflow; the pod may still be Pending
#   running    an implementer pod is executing
PHASE_ADMITTED = "admitted"
PHASE_SUBMITTED = "submitted"
PHASE_RUNNING = "running"


# Heartbeat sentinel for "the POST succeeded (raise_for_status didn't raise,
# so Argo already created the run server-side) but the response body couldn't
# be parsed for a workflow name" — an edge case narrower than a submit
# failure: the SDK run already exists, we just don't know its name. A retry
# that sees this must NOT resubmit (that would duplicate the real SDK run,
# the one failure mode this whole resume design exists to prevent) — it
# fails loudly instead, for manual recovery, which is a strictly better
# outcome than a silent duplicate.
_SUBMITTED_UNKNOWN_NAME = "<submitted, workflow name unparseable>"


def _now_iso() -> str:
    return _iso(datetime.now(UTC)) or ""


def _iso(moment: datetime | None) -> str | None:
    """One spelling of an instant across the whole projection.

    `datetime.isoformat()` alone would put `+00:00` on a tz-aware value and
    no offset at all on a naive one, so the three timestamps in a heartbeat
    could arrive in three spellings and leave the consumer (#389) parsing
    all of them.
    """
    if moment is None:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


@activity.defn
async def submit_and_wait(input: SubmitAndWaitInput) -> WorkflowResult:
    headers = auth_headers()
    async with httpx.AsyncClient(base_url=MCTL_API_BASE_URL, timeout=REQUEST_TIMEOUT_SECONDS) as client:
        # Resume, don't resubmit: a prior attempt of THIS activity execution
        # already heartbeated a workflow_name once submission succeeded (see
        # below). If this attempt has that detail, the Argo run already
        # exists — go straight to polling instead of POSTing a second one.
        info = activity.info()
        heartbeat_details = info.heartbeat_details
        # Detail [0] is the workflow name and is the resume key; everything
        # else is runtime projection and must never be read back to decide
        # anything. Keeping the name first keeps a retry of an activity that
        # heartbeated under the older single-detail shape resumable.
        workflow_name = heartbeat_details[0] if heartbeat_details else None
        runtime: dict[str, str | None] = {
            "phase": PHASE_ADMITTED,
            "admitted_at": _iso(info.started_time),
            "submitted_at": None,
            "implementer_started_at": None,
        }
        # A resumed attempt starts from the projection the previous attempt
        # left, not from scratch: the next heartbeat overwrites the details
        # wholesale, so rebuilding a fresh `admitted` dict here would erase
        # `submitted_at` and `implementer_started_at` from what #389 reads,
        # and would report a workflow already running as merely admitted.
        # Anything unreadable falls through to the fresh dict below, and
        # only keys this shape defines are taken, so a detail written by a
        # future version cannot inject fields.
        prior = heartbeat_details[1] if len(heartbeat_details) > 1 else None
        if workflow_name:
            if isinstance(prior, dict):
                runtime.update({k: v for k, v in prior.items() if k in runtime})
            # Submitted is a floor on every resume, including one from an
            # attempt that heartbeated under the older name-only shape: the
            # resume key exists only because a previous attempt got past the
            # POST.
            if runtime["phase"] == PHASE_ADMITTED:
                runtime["phase"] = PHASE_SUBMITTED
        is_implement = input.operation == IMPLEMENTATION_OPERATION

        if workflow_name == _SUBMITTED_UNKNOWN_NAME:
            raise RuntimeError(
                f"a prior attempt submitted {input.operation} but its Argo workflow name could not be "
                "determined from mctl-api's response — refusing to resubmit (would duplicate the real SDK "
                "run); check Argo/mctl-api for a recent run of this operation and record it manually"
            )

        if workflow_name:
            activity.logger.info(
                "resuming poll for %s -> %s (retry after crash/heartbeat gap, no re-submit)",
                input.operation,
                workflow_name,
            )
        else:
            submit_resp = await client.post(
                f"/api/v1/operations/{input.operation}/execute",
                json=input.params,
                headers=headers,
            )
            submit_resp.raise_for_status()
            try:
                workflow_name = submit_resp.json()["workflow"]["workflowName"]
            except (ValueError, KeyError, TypeError) as exc:
                activity.heartbeat(_SUBMITTED_UNKNOWN_NAME)
                raise RuntimeError(
                    f"submitted {input.operation} (mctl-api accepted it) but could not parse the workflow "
                    f"name from its response: {exc!r}"
                ) from exc
            activity.logger.info("submitted %s -> %s", input.operation, workflow_name)
            # Record the submission immediately, before the first poll, so
            # even a crash on the very next line leaves a retry able to find
            # workflow_name via heartbeat_details above instead of resubmitting.
            runtime["phase"] = PHASE_SUBMITTED
            runtime["submitted_at"] = _now_iso()
            activity.heartbeat(workflow_name, dict(runtime))

        consecutive_errors = 0
        while True:
            # Heartbeat before every poll, not just on change: a stuck
            # mctl-api / cluster makes this loop spin on the `continue`
            # below without ever reaching a terminal phase, and the
            # heartbeat is what lets Temporal notice a genuinely wedged
            # activity (worker crash, network partition) instead of the
            # activity looking alive forever because polling itself is
            # still succeeding.
            activity.heartbeat(workflow_name, dict(runtime))

            try:
                status_resp = await client.get(f"/api/v1/workflows/{workflow_name}", headers=headers)
                status_resp.raise_for_status()
            except (httpx.HTTPStatusError, httpx.TransportError) as exc:
                consecutive_errors += 1
                if consecutive_errors > MAX_CONSECUTIVE_POLL_ERRORS:
                    activity.logger.error(
                        "giving up polling %s after %d consecutive failures: %s",
                        workflow_name,
                        consecutive_errors,
                        exc,
                    )
                    raise
                activity.logger.warning(
                    "poll failed for %s (%d/%d consecutive): %s",
                    workflow_name,
                    consecutive_errors,
                    MAX_CONSECUTIVE_POLL_ERRORS,
                    exc,
                )
                await asyncio.sleep(POLL_INTERVAL_SECONDS)
                continue

            consecutive_errors = 0
            body = status_resp.json()
            live = body.get("live") or {}
            status_block = live.get("status") or {}
            phase = status_block.get("phase", "")

            observation = observe_implementer(status_block) if is_implement else None
            if observation is not None and observation.ran and runtime["phase"] != PHASE_RUNNING:
                # The attempt begins HERE — when a pod is known to have run —
                # not at approval, not at admission, not at Argo accepting
                # the workflow. Between submitted and running sits a real
                # class of failure (accepted, never scheduled), and it is
                # still pre-start.
                runtime["phase"] = PHASE_RUNNING
                runtime["implementer_started_at"] = observation.started_at
                activity.logger.info(
                    "%s -> %s: implementer pod running since %s",
                    input.operation,
                    workflow_name,
                    observation.started_at,
                )

            if phase in TERMINAL_PHASES:
                return WorkflowResult(
                    workflow_name=workflow_name,
                    phase=phase,
                    started_at=status_block.get("startedAt"),
                    finished_at=status_block.get("finishedAt"),
                    implementer_ran=observation.ran if observation else None,
                    implementer_phase=observation.phase if observation else None,
                    implementer_started_at=observation.started_at if observation else None,
                    finalization_phase=observation.finalization_phase if observation else None,
                )

            await asyncio.sleep(POLL_INTERVAL_SECONDS)
