"""DevLoopWorkflow: the durable orchestrator for one GitHub issue's path
through investigate -> human approval -> implement.

This is the phase-4 "first vertical slice" from the plan, deliberately
narrower than the full issue -> ... -> deploy -> monitor pipeline: it covers
exactly the acceptance slice the plan defines ("mctl_trigger_issue ->
Temporal -> registry resolve -> Argo investigate -> proposal in gitops ->
approval signal -> implement -> PR"). The review/fix loop (shepherd) and
deploy/monitor stages are phase 5/6 work, layered on top of this workflow
once the slice is proven, not reimplemented here.

Approval is atomic as of the phase-5 cutover (mctl-agents#150): `approve()`
unblocks this workflow's wait_condition AND the workflow then submits the
`mctl-agents-approve` CWFT, which flips exactly this issue's proposal from
`proposed` to `accepted` as a gitops commit under the
`mctl-gitops-main-writes` mutex. The flip runs inside Argo because this
worker deliberately holds no gitops checkout or deploy key — every gitops
write must go through Argo. The signal optionally carries the approver's
identity, which lands in the proposal's approval block, the gitops commit
message, and the execution audit trail. The flip CWFT is idempotent
(already-accepted is a successful no-op), so a Temporal retry or a racing
manual approve never fails the loop. Histories recorded before this change
replay the legacy signal-only branch via workflow.patched("atomic-approve");
for those in-flight loops the manual gitops flip remains the affordance.

The implement step stays scoped to this issue's own repo AND its own
proposal slug (see `_target_repo` and `find_proposal_slug` below), so even
a mis-signalled approve can never implement a different repo's — or a
different issue's — proposal.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import (
    ActivityError,
    ApplicationError,
)
from temporalio.exceptions import (
    TimeoutError as TemporalTimeoutError,
)

with workflow.unsafe.imports_passed_through():
    from orchestrator.temporal.activities.argo import SubmitAndWaitInput, WorkflowResult, submit_and_wait
    from orchestrator.temporal.activities.deploy_state import (
        DeployStatus,
        DeployTarget,
        ReleaseInfo,
        get_deploy_status,
        get_release_after,
        resolve_deploy_target,
    )
    from orchestrator.temporal.activities.pr_state import PRState, get_pr_state
    from orchestrator.temporal.activities.proposals import find_proposal_slug
    from orchestrator.temporal.activities.registry import ResolvedRelease, resolve_agent_release
    from orchestrator.temporal.activities.state import ExecutionRecord, record_execution
    from orchestrator.temporal.issue_ref import parse_issue_url

ENVIRONMENT = "production"

# The Argo CWFTs already retry within a run (second-OAuth-account fallback on
# a 429/five_hour limit) — see activities/argo.py's module docstring. A
# Temporal retry that re-submitted on top of that would multiply real SDK
# runs, so this bound only exists to let a retried attempt RESUME polling
# (via activity.info().heartbeat_details, see submit_and_wait) after a worker
# crash or missed heartbeat — submit_and_wait detects "already submitted" and
# refuses to re-POST once it has a workflow_name (including the "submitted
# but name unparseable" sentinel, which fails loudly instead of guessing).
# The only gap this doesn't close: a crash strictly between the Argo POST
# succeeding and the first activity.heartbeat() call actually landing at the
# Temporal server — vanishingly small (no I/O in between), but real. 3, not
# unlimited: still fail loudly on a genuinely wedged activity rather than
# retry forever.
SDK_STEP_RETRY_POLICY = RetryPolicy(maximum_attempts=3)
SDK_STEP_TIMEOUT = timedelta(hours=2)
SDK_STEP_HEARTBEAT_TIMEOUT = timedelta(minutes=2)

FAST_ACTIVITY_TIMEOUT = timedelta(seconds=30)
# Bounded, not the Temporal default of unlimited attempts: resolve/record are
# plain HTTP round trips to mctl-api, so a handful of retries covers a real
# transient blip. Unlimited retries on _record in particular would otherwise
# wedge wait_condition forever if mctl-api's executions endpoint were ever
# down — the real SDK work (submit_and_wait) already succeeded by the time
# _record runs, so its own failure must never block workflow progress (see
# _record's try/except below).
FAST_ACTIVITY_RETRY_POLICY = RetryPolicy(maximum_attempts=5)

# find_proposal_slug hits GitHub, not mctl-api: its worst realistic failure
# is a rate limit whose reset window is up to an hour. FAST (5 attempts,
# default backoff) burns through in under a minute and would permanently
# fail the workflow over a transient limit — reintroducing the manual
# re-trigger toil this fix exists to remove. Spread bounded retries across
# well over an hour instead; the activity is one cheap GET, so patience is
# free. Still bounded: a genuinely broken lookup must eventually surface.
SLUG_LOOKUP_TIMEOUT = timedelta(seconds=30)
SLUG_LOOKUP_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=15),
    maximum_attempts=12,
)

# The approve CWFT is clone + one-line flip + push — no agent, no SDK, and
# its own activeDeadlineSeconds is 600. Anything near SDK_STEP_TIMEOUT here
# would just mean a wedged mutex holding the loop for hours.
APPROVE_STEP_TIMEOUT = timedelta(minutes=15)

# Stage 6.1 merge detection (ADR-006, #214): after implement, poll the PR's
# state until it merges/closes. Two cheap GitHub reads per poll — 30 min is
# responsive enough for a merge event nothing downstream reacts to in
# real time yet, and ~2.9k history events over the full 14-day deadline
# stays far under Temporal's 50k event limit. The deadline bounds the
# workflow's lifetime: a PR still open after two weeks is returned as-is
# (state="OPEN"), not waited on forever.
MERGE_POLL_INTERVAL = timedelta(minutes=30)
MERGE_WATCH_DEADLINE = timedelta(days=14)
# The implementer writes the pr: link into .status.yaml in the same commit
# that flips it to implemented, so the link should be visible on the first
# poll. A few polls of grace absorb gitops main lag; after that, a missing
# link means the status write failed — give up rather than poll for 14 days.
PR_LOOKUP_GRACE_POLLS = 4
PR_STATE_TIMEOUT = timedelta(minutes=2)
PR_STATE_RETRY_POLICY = RetryPolicy(maximum_attempts=5)

# Stage 6.1 review loop (#213): while the PR stays open, the workflow runs
# its OWN shepherd ticks instead of relying on the global cron (which
# narrows to a sweeper and skips slugs a running DevLoop owns — see
# run_shepherd._dev_loop_owns). Cadence: every 8th poll ≈ 4 h — each tick
# provisions a Hetzner volume, so hours not minutes (ADR-006 cost note),
# and never on the first poll (claude review auto-fires on PR open; an
# immediate tick would just observe "review pending"). Capped: after
# SHEPHERD_TICKS_MAX active ticks the loop keeps watching passively —
# the shepherd itself flips review-stuck after 3 address-review attempts,
# so a stuck PR must not burn a volume every 4 h for two weeks.
SHEPHERD_TICK_EVERY_POLLS = 8
SHEPHERD_TICKS_MAX = 12

# Stages 6.2/6.3 (ADR-006, #215). After the PR merges, release-please cuts
# a release on the app repo, that dispatches mctl-gitops release-deploy,
# which bumps the image tag and ArgoCD auto-syncs within ~30 s. The loop
# only watches; it never drives any of it.
#
# Two separate waits, because they fail for different reasons. A release
# that never appears means release-please had nothing to release (a
# docs-only or non-conventional merge) — common, and settled within a few
# minutes, so a short window. A release that appeared but has not gone
# Healthy is a real rollout in progress: image build, gitops commit, sync,
# rollout, probes — minutes, occasionally much longer under a busy runner,
# so a wider window before giving up as "unverified".
RELEASE_LOOKUP_DEADLINE = timedelta(minutes=20)
RELEASE_POLL_INTERVAL = timedelta(minutes=2)
DEPLOY_VERIFY_DEADLINE = timedelta(minutes=45)
DEPLOY_POLL_INTERVAL = timedelta(minutes=1)
DEPLOY_READ_TIMEOUT = timedelta(minutes=2)
DEPLOY_READ_RETRY_POLICY = RetryPolicy(maximum_attempts=5)


@dataclass(frozen=True)
class IssueRef:
    issue_url: str


@dataclass(frozen=True)
class DeployObservation:
    """Outcome of watching this merge's release reach the cluster.

    ``outcome`` is one of:

    - ``healthy``    — the app reports Synced/Healthy on the released tag
    - ``unverified`` — the deadline passed first; ``detail`` says what the
      last read showed. NOT a workflow failure: the deploy may still be
      progressing, and this loop never rolls anything back
    - ``no-release`` — release-please cut nothing for this merge (docs-only
      or non-conventional commits); nothing to observe, and not a fault
    - ``no-target``  — the repo's release deploys no application (it only
      bumps cluster templates, or has no release-please dispatch)
    """

    outcome: str
    team: str | None = None
    app: str | None = None
    release_tag: str | None = None
    image_tag: str | None = None
    health: str | None = None
    sync_status: str | None = None
    detail: str | None = None


@dataclass(frozen=True)
class DevLoopResult:
    investigate: WorkflowResult
    # None if approval was never signalled, investigate failed, or the
    # approve flip failed (see `approve` below to tell those apart).
    implement: WorkflowResult | None
    # None on pre-atomic-approve histories (legacy manual flip) and when the
    # workflow never reached the approve stage. Defaulted so results recorded
    # before this field existed still deserialize.
    approve: WorkflowResult | None = None
    # Stage 6.1 merge detection (ADR-006, #214): the last PR state observed
    # by the post-implement polling loop. None on pre-merge-detection
    # histories and when implement never produced a PR to watch; a PRState
    # with state="OPEN" means the watch deadline expired with the PR still
    # open (the workflow does not wait forever).
    pr: PRState | None = None
    # Stages 6.2/6.3 (ADR-006, #215): what happened to the release this
    # merge produced. None on histories predating the stage and whenever
    # the PR did not merge. See DeployObservation for the outcomes.
    deploy: DeployObservation | None = None


async def _resolve(agent: str) -> ResolvedRelease | None:
    return await workflow.execute_activity(
        resolve_agent_release,
        args=[agent, ENVIRONMENT],
        start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
        retry_policy=FAST_ACTIVITY_RETRY_POLICY,
    )


async def _run_cwft(
    operation: str, params: dict[str, str], *, step_timeout: timedelta = SDK_STEP_TIMEOUT
) -> WorkflowResult:
    return await workflow.execute_activity(
        submit_and_wait,
        SubmitAndWaitInput(operation=operation, params=params),
        start_to_close_timeout=step_timeout,
        heartbeat_timeout=SDK_STEP_HEARTBEAT_TIMEOUT,
        retry_policy=SDK_STEP_RETRY_POLICY,
    )


def _target_repo(issue: IssueRef) -> str:
    return parse_issue_url(issue.issue_url).repo


async def _record(
    agent: str, release: ResolvedRelease | None, result: WorkflowResult, target_repo: str
) -> None:
    # A release with no image_ref means resolve_agent_release found nothing
    # to pin, so the CWFT ran its own baked-in default image instead (see
    # activities/registry.py) — record that as "no pinned version", not as
    # release.version, or the audit trail would claim a specific registry
    # version produced this result when it was never actually passed to the
    # run.
    pinned = release if release and release.image_ref else None

    # Best-effort: this is an audit-trail write, not the real work (the CWFT
    # this records already ran to completion by the time this is called).
    # Letting a persistent failure here fail the whole workflow would make
    # an mctl-api outage on this one endpoint block wait_condition/approval
    # indefinitely for work that already succeeded — worse than a missing
    # execution record.
    try:
        await workflow.execute_activity(
            record_execution,
            ExecutionRecord(
                temporal_workflow_id=workflow.info().workflow_id,
                agent=agent,
                environment=ENVIRONMENT,
                version=pinned.version if pinned else "",
                image_ref=pinned.image_ref if pinned else "",
                target_repo=target_repo,
                argo_workflow_name=result.workflow_name,
                phase=result.phase,
            ),
            start_to_close_timeout=FAST_ACTIVITY_TIMEOUT,
            retry_policy=FAST_ACTIVITY_RETRY_POLICY,
        )
    except ActivityError:
        workflow.logger.warning(
            "record_execution failed after retries for agent=%s argo_workflow=%s — "
            "continuing without a durable execution record for this step",
            agent,
            result.workflow_name,
        )


def _is_transient(exc: ActivityError) -> bool:
    """Is this activity failure a known-transient one, or a bug?

    The read activities wrap every expected failure in
    ProposalListingError, and an activity timeout is transport by
    definition. Anything else escaping an activity is an unexpected defect
    — retrying THAT for the rest of a long deadline would silently mask
    it, which is the lesson _watch_pr already learned (agy P2, round 3 on
    #224).
    """
    cause = exc.cause
    return isinstance(cause, TemporalTimeoutError) or (
        isinstance(cause, ApplicationError) and cause.type == "ProposalListingError"
    )


def _drain_tick(tick_task: asyncio.Task[None], service: str, slug: str) -> None:
    """Retrieve a finished tick's exception so it is never swallowed.

    _shepherd_tick catches its own failures, so this should find nothing —
    but a task whose exception is never retrieved disappears silently
    (with only an asyncio "exception was never retrieved" warning), and
    the watch loop drops the reference on the next poll. Reading it is the
    cheap way to guarantee that cannot happen.
    """
    if tick_task.cancelled():
        return
    exc = tick_task.exception()
    if exc is not None:
        workflow.logger.error(
            "in-loop shepherd tick for %s/%s ended with an unretrieved %s: %r",
            service,
            slug,
            type(exc).__name__,
            exc,
        )


@workflow.defn
class DevLoopWorkflow:
    def __init__(self) -> None:
        self._approved = False
        self._approver: str | None = None
        self._shepherd_in_loop = False

    @workflow.query
    def shepherd_in_loop(self) -> bool:
        """Does THIS execution run its own shepherd ticks? (#213)

        The shepherd cron asks mctl-api, which asks this query, because
        "Running" alone cannot answer it: an execution that started before
        the `shepherd-in-loop` patch replays that branch as False and will
        never tick, yet stays Running until the merge-watch deadline. The
        cron must keep sweeping those proposals, so it needs the patch
        marker this workflow actually recorded — not its status.
        """
        return self._shepherd_in_loop

    @workflow.signal
    def approve(self, *args: object) -> None:
        # Optional payload for the audit trail: legacy senders signal with no
        # args (still valid), new senders pass {"approver": "..."} or a bare
        # approver string. Signals must never raise, so parse defensively and
        # treat anything unrecognized as an approve with no identity.
        for arg in args:
            if isinstance(arg, dict):
                approver = arg.get("approver")
                if isinstance(approver, str) and approver:
                    self._approver = approver
            elif isinstance(arg, str) and arg:
                self._approver = arg
        self._approved = True

    @workflow.run
    async def run(self, issue: IssueRef) -> DevLoopResult:
        target_repo = _target_repo(issue)

        # Pin the investigator version ONCE, at the start of this step. A
        # later promote/rollback in the registry must not retroactively
        # change what an in-flight (or replayed) workflow already ran.
        investigator_release = await _resolve("issue-investigator")
        investigate_params = {"issue_url": issue.issue_url}
        if investigator_release and investigator_release.image_ref:
            investigate_params["agent_image"] = investigator_release.image_ref
            investigate_params["agent_version"] = f"issue-investigator@{investigator_release.version}"

        investigate_result = await _run_cwft("mctl-agents-investigate", investigate_params)
        await _record("issue-investigator", investigator_release, investigate_result, target_repo)

        if not investigate_result.succeeded:
            return DevLoopResult(investigate=investigate_result, implement=None)

        # Durable wait: this workflow can sit here for days without costing
        # anything beyond Temporal's own history storage — exactly the
        # "durable per-issue state" the plan's problem statement calls out
        # as missing from the current polling-cron pipeline.
        await workflow.wait_condition(lambda: self._approved)

        # Scoped to this issue's own proposal, not just its repo. Service
        # scoping alone left a same-repo race: two approved loops for the
        # same repo both discovered the full accepted list from their own
        # (stale) gitops clones, claimed overlapping proposals, and their
        # commit-and-push steps rebase-conflicted on each other's
        # .status.yaml (2026-08-28, mctl-portal 79/80 — mctl-agents#203).
        # With the slug pinned, concurrent same-repo loops touch disjoint
        # proposal dirs and the commit step's rebase-retry stays clean.
        #
        # Failing loudly beats falling back to an unscoped run: unscoped,
        # this loop could implement a DIFFERENT accepted proposal in the
        # repo — the exact wrong-proposal hazard scoping exists to prevent.
        # The proposal must exist by now (investigate committed it before
        # this workflow ever reached wait_condition), so a missing slug
        # after retries means agents-state is in a state a human needs to
        # look at anyway.
        # workflow.patched: histories recorded before this change scheduled
        # submit_and_wait directly after the implementer resolve — replaying
        # them through an unconditional find_proposal_slug would be a command
        # mismatch (nondeterminism) that permanently wedges every in-flight
        # approved loop at deploy time. Old histories take the legacy
        # unscoped branch; new executions record the patch marker and get
        # slug scoping. Drop to workflow.deprecate_patch once no pre-patch
        # execution can still be running.
        slug: str | None = None
        approve_result: WorkflowResult | None = None
        implementer_release: ResolvedRelease | None = None
        # Evaluate the atomic-approve patch ONCE, up front, because it also
        # decides the position of the implementer resolve. Histories from the
        # slug-scoped-but-pre-approve era (1.29.2..1.29.4) recorded
        # resolve → slug-lookup → implement; unconditionally moving the
        # resolve after the flip would be a command mismatch that wedges
        # every such in-flight loop on replay (codex P1 round 2, PR #212).
        # patched() returns a stable answer per execution, so both branches
        # below see the same value.
        atomic_approve = workflow.patched("atomic-approve")
        if not atomic_approve:
            # Legacy position: pre-atomic-approve histories resolved the
            # implementer before the slug lookup — keep their command order.
            implementer_release = await _resolve("implementer")
        if workflow.patched("slug-scoped-implement"):
            issue_number = parse_issue_url(issue.issue_url).number
            slug = await workflow.execute_activity(
                find_proposal_slug,
                args=[target_repo, issue_number],
                start_to_close_timeout=SLUG_LOOKUP_TIMEOUT,
                retry_policy=SLUG_LOOKUP_RETRY_POLICY,
            )
            if not slug:
                raise ApplicationError(
                    f"no proposal dir issue-{issue_number}-* found under "
                    f"agents-state/{target_repo}/proposals on gitops main; "
                    "refusing an unscoped implement run",
                    non_retryable=True,
                )
            # Atomic approve (phase-5 cutover, mctl-agents#150): flip THIS
            # proposal proposed → accepted as an Argo-executed gitops commit,
            # instead of relying on the operator's manual edit. Nested inside
            # the slug-scoped branch because the flip needs the slug, and any
            # execution new enough to record this patch marker records the
            # outer one too; old in-flight histories replay the legacy
            # signal-only path (manual flip stays their affordance). The CWFT
            # is idempotent on already-accepted, so a Temporal retry or a
            # racing manual approve is a successful no-op — but any other
            # failure (missing proposal dir, unexpected status, push failure)
            # stops the loop HERE: proceeding to implement without a durable
            # accepted status would just be a silent no-op run.
            if atomic_approve:
                approve_result = await _run_cwft(
                    "mctl-agents-approve",
                    {
                        "service": target_repo,
                        "slug": slug,
                        "approver": self._approver or "unknown",
                    },
                    step_timeout=APPROVE_STEP_TIMEOUT,
                )
                # No record_execution here: the executions ledger is for
                # SDK-backed agent runs (see docs/agent-inventory.yaml), and
                # this deterministic flip is already triply audited — the
                # .status.yaml approval block, the gitops commit message,
                # and this workflow's own history all carry the approver.
                if not approve_result.succeeded:
                    return DevLoopResult(
                        investigate=investigate_result,
                        implement=None,
                        approve=approve_result,
                    )
        if atomic_approve:
            # Resolve the implementer only AFTER the approval flip is
            # durable: _resolve can fail permanently (registry outage
            # outlasting its five retries), and if that happened before the
            # flip, the operator's approval would evaporate with the failed
            # workflow — and the REJECT_DUPLICATE start policy would turn a
            # transient outage into a permanently stuck issue (codex P1 on
            # PR #212). Pre-atomic-approve histories already resolved above,
            # in their recorded position.
            implementer_release = await _resolve("implementer")
        #
        # Depends on mctl-gitops's cwft-mctl-agents-implement.yaml already
        # declaring this `service` parameter and threading it to
        # `run_implementer.py --service <value>` — verified directly
        # (not assumed) as of this comment: see
        # cwft-mctl-agents-implement.yaml's `arguments.parameters` (name:
        # service) and its `implement-proposals` step, which does
        # `[ -n "$WORKFLOW_SERVICE" ] && set -- "$@" --service "$WORKFLOW_SERVICE"`
        # before invoking run_implementer.py. mctl-agents' CI can't check
        # this out to assert it directly (mctl-gitops is a sibling repo), so
        # if that CWFT is ever changed to drop/rename the parameter, this
        # scoping silently reverts to today's unscoped behavior with no
        # error on this side.
        implement_params: dict[str, str] = {"service": target_repo}
        if slug:
            implement_params["slug"] = slug
        if implementer_release and implementer_release.image_ref:
            implement_params["agent_image"] = implementer_release.image_ref
            implement_params["agent_version"] = f"implementer@{implementer_release.version}"

        implement_result = await _run_cwft("mctl-agents-implement", implement_params)
        await _record("implementer", implementer_release, implement_result, target_repo)

        # Stage 6.1 merge detection (ADR-006, #214): watch the implement PR
        # until it merges/closes, bounded by MERGE_WATCH_DEADLINE. Requires
        # the slug (the PR is resolved from this proposal's .status.yaml);
        # any execution new enough to record this marker also recorded
        # slug-scoped-implement, so slug is set whenever the branch is taken.
        pr_state: PRState | None = None
        if workflow.patched("merge-detection") and implement_result.succeeded and slug:
            pr_state = await self._watch_pr(target_repo, slug)

        # Stages 6.2/6.3 (ADR-006, #215): only a merged PR produces a
        # release to observe. A closed-unmerged or still-open PR ends the
        # loop here, exactly as before.
        deploy: DeployObservation | None = None
        if (
            workflow.patched("deploy-observation")
            and pr_state is not None
            and pr_state.merged
        ):
            deploy = await self._observe_deploy(target_repo, pr_state)

        return DevLoopResult(
            investigate=investigate_result,
            implement=implement_result,
            approve=approve_result,
            pr=pr_state,
            deploy=deploy,
        )

    async def _observe_deploy(self, service: str, pr_state: PRState) -> DeployObservation:
        """Watch this merge's release land, then verify the rollout (#215).

        Read-only and non-fatal by construction: every unhappy path
        returns a DeployObservation describing what was seen. implement
        already succeeded and the PR already merged — an unobserved deploy
        must not turn that into a failed workflow, and this loop has no
        rollback to offer (wft-rollback-service stays human-invoked).
        """
        repo = pr_state.repo or f"mctlhq/{service}"
        try:
            target: DeployTarget | None = await workflow.execute_activity(
                resolve_deploy_target,
                args=[repo],
                start_to_close_timeout=DEPLOY_READ_TIMEOUT,
                retry_policy=DEPLOY_READ_RETRY_POLICY,
            )
        except ActivityError as exc:
            return DeployObservation(
                outcome="unverified",
                detail=f"could not resolve the deploy target for {repo}: {exc.cause!r}",
            )
        if target is None:
            return DeployObservation(
                outcome="no-target",
                detail=f"{repo} releases deploy no application",
            )

        # merged_at is absent on PRStates recorded before the field
        # existed; the merge commit is still minutes old at worst here, so
        # anchoring on "now" only risks missing a release cut in the same
        # instant, which the next poll picks up anyway.
        after = pr_state.merged_at or workflow.now().isoformat().replace("+00:00", "Z")
        release, failure = await self._await_release(repo, after)
        if failure is not None:
            # A broken read is not the same as nothing being released, and
            # labelling it no-release would hide a defect behind an
            # outcome that reads as normal (agy P2).
            return DeployObservation(
                outcome="unverified",
                team=target.team,
                app=target.app,
                detail=failure,
            )
        if release is None:
            return DeployObservation(
                outcome="no-release",
                team=target.team,
                app=target.app,
                detail=f"no release published for {repo} within {RELEASE_LOOKUP_DEADLINE}",
            )
        return await self._verify_rollout(target, release)

    async def _await_release(self, repo: str, after: str) -> tuple[ReleaseInfo | None, str | None]:
        """Poll until release-please publishes a release newer than ``after``.

        Returns (release, failure). A failure string means the lookup
        itself broke; the caller must not read that as "nothing was
        released", which is what a bare None would have looked like.
        """
        deadline = workflow.now() + RELEASE_LOOKUP_DEADLINE
        while workflow.now() < deadline:
            try:
                release: ReleaseInfo | None = await workflow.execute_activity(
                    get_release_after,
                    args=[repo, after],
                    start_to_close_timeout=DEPLOY_READ_TIMEOUT,
                    retry_policy=DEPLOY_READ_RETRY_POLICY,
                )
            except ActivityError as exc:
                if not _is_transient(exc):
                    workflow.logger.warning(
                        "release lookup for %s failed with a non-transient error — "
                        "giving up on the release watch: %r",
                        repo,
                        exc.cause,
                    )
                    return None, f"release lookup failed with a non-transient error: {exc.cause!r}"
                workflow.logger.warning(
                    "release lookup failed for %s — retrying next interval: %r", repo, exc.cause
                )
                await workflow.sleep(RELEASE_POLL_INTERVAL)
                continue
            if release is not None:
                return release, None
            await workflow.sleep(RELEASE_POLL_INTERVAL)
        return None, None

    async def _verify_rollout(self, target: DeployTarget, release: ReleaseInfo) -> DeployObservation:
        """Wait until the app reports Synced/Healthy on the released tag."""
        deadline = workflow.now() + DEPLOY_VERIFY_DEADLINE
        last: DeployStatus | None = None
        while workflow.now() < deadline:
            try:
                status: DeployStatus = await workflow.execute_activity(
                    get_deploy_status,
                    args=[target.team, target.app],
                    start_to_close_timeout=DEPLOY_READ_TIMEOUT,
                    retry_policy=DEPLOY_READ_RETRY_POLICY,
                )
            except ActivityError as exc:
                if not _is_transient(exc):
                    return DeployObservation(
                        outcome="unverified",
                        team=target.team,
                        app=target.app,
                        release_tag=release.tag,
                        detail=f"deploy status read failed with a non-transient error: {exc.cause!r}",
                    )
                workflow.logger.warning(
                    "deploy status read failed for %s/%s — retrying next interval: %r",
                    target.team,
                    target.app,
                    exc.cause,
                )
                await workflow.sleep(DEPLOY_POLL_INTERVAL)
                continue
            if not status.found:
                # The name resolves to no ArgoCD application. Polling it
                # for 45 minutes would only delay the same answer.
                return DeployObservation(
                    outcome="unverified",
                    team=target.team,
                    app=target.app,
                    release_tag=release.tag,
                    detail=f"no ArgoCD application {target.team}/{target.app}",
                )
            last = status
            # mctl-api resolves no service record — and therefore no image
            # tag — for platform applications such as mctl-api itself.
            # Waiting for a tag that will never be reported would make
            # every such loop time out, so sync+health is the signal there.
            if status.image_tag is not None:
                landed = status.image_tag == release.tag
            else:
                # No service record, so no tag to compare (mctl-api's own
                # platform application). Healthy/Synced alone would be
                # satisfied by the state the app was ALREADY in before this
                # release synced, so the first poll would report success
                # without anything having happened (claude P3). ArgoCD's
                # own updatedAt is the freshness signal: it must be at
                # least as new as the release we are waiting for.
                landed = status.updated_at is not None and status.updated_at >= release.published_at
            if landed and status.health == "Healthy" and status.sync_status == "Synced":
                return DeployObservation(
                    outcome="healthy",
                    team=target.team,
                    app=target.app,
                    release_tag=release.tag,
                    image_tag=status.image_tag,
                    health=status.health,
                    sync_status=status.sync_status,
                )
            await workflow.sleep(DEPLOY_POLL_INTERVAL)
        return DeployObservation(
            outcome="unverified",
            team=target.team,
            app=target.app,
            release_tag=release.tag,
            image_tag=last.image_tag if last else None,
            health=last.health if last else None,
            sync_status=last.sync_status if last else None,
            detail=f"still not Synced/Healthy on {release.tag} after {DEPLOY_VERIFY_DEADLINE}",
        )

    async def _shepherd_tick(self, service: str, slug: str) -> None:
        """One in-loop shepherd run for exactly this proposal (#213).

        The same one-shot the cron ran, scoped to service+slug (which puts
        run_shepherd in targeted mode, bypassing the ownership filter). A
        failed tick is logged, not raised: the watch itself is the durable
        part, and the next boundary retries.
        """
        try:
            # Pin the released shepherd image exactly like the
            # investigate/implement steps do, so a promotion or rollback in
            # the agent registry reaches in-loop ticks too — otherwise
            # DevLoop-owned proposals would silently run the CWFT's
            # baked-in default while cron-driven ones ran the intended
            # release.
            shepherd_release = await _resolve("shepherd")
            tick_params = {"service": service, "slug": slug}
            if shepherd_release and shepherd_release.image_ref:
                tick_params["agent_image"] = shepherd_release.image_ref
                tick_params["agent_version"] = f"shepherd@{shepherd_release.version}"
            tick_result = await _run_cwft("mctl-agents-shepherd", tick_params)
            await _record("shepherd", shepherd_release, tick_result, service)
        except ActivityError as exc:
            workflow.logger.warning(
                "in-loop shepherd tick failed for %s/%s — the watch "
                "continues: %r",
                service,
                slug,
                exc.cause,
            )
        except Exception as exc:  # noqa: BLE001 — a bug in the tick must not sink silently
            # Running as a task means an escaping exception is stored on
            # the task instead of raised: nothing would surface it, and
            # the next poll overwrites the reference. Log it here, at the
            # only point that still has the context.
            workflow.logger.error(
                "in-loop shepherd tick for %s/%s raised an unexpected %s: %r",
                service,
                slug,
                type(exc).__name__,
                exc,
            )

    async def _settle_tick(self, tick_task: asyncio.Task[None] | None, service: str, slug: str) -> None:
        """Leave no pending tick behind when the watch ends (#231).

        A workflow that completes with an outstanding task is a Temporal
        error, and once the PR is merged/closed the tick's verdict is moot
        — so cancel rather than wait, which is the whole point of running
        it concurrently. Cancelling the activity does not stop the Argo
        workflow it submitted; that run finishes on its own, exactly as it
        would have if this loop had never waited for it.
        """
        if tick_task is None:
            return
        if tick_task.done():
            _drain_tick(tick_task, service, slug)
            return
        workflow.logger.info(
            "cancelling an in-flight shepherd tick for %s/%s — the watch is ending",
            service,
            slug,
        )
        tick_task.cancel()
        try:
            await tick_task
        except asyncio.CancelledError:
            # The expected outcome of the cancel above. CancelledError is a
            # BaseException since 3.8, so `except Exception` does NOT catch
            # it: letting it escape here would propagate out of _watch_pr's
            # finally, discard the state the watch was about to return, and
            # fail a workflow whose implement and merge both succeeded —
            # in exactly the case this concurrency exists to handle.
            workflow.logger.debug("in-flight shepherd tick cancelled for %s/%s", service, slug)
        except Exception as exc:  # noqa: BLE001 — whatever the tick raised on its way out
            workflow.logger.warning(
                "in-flight shepherd tick for %s/%s ended with %r", service, slug, exc
            )

    async def _watch_pr(self, service: str, slug: str) -> PRState | None:
        """Poll get_pr_state until the PR reaches a terminal state.

        Observational, fail-open: a persistently failing read (GitHub outage
        outlasting the activity retries) returns the last known state
        instead of failing a loop whose implement already succeeded. Returns
        None when no PR link ever appeared within the grace polls.
        """
        deadline = workflow.now() + MERGE_WATCH_DEADLINE
        polls_without_pr = 0
        last: PRState | None = None
        # In-loop shepherd (#213): evaluated once — the marker also fixes
        # whether tick commands appear in this execution's history at all.
        shepherd_in_loop = workflow.patched("shepherd-in-loop")
        # Published to the sweeper via the shepherd_in_loop query the moment
        # the watch starts, not at the first tick 4 h later: between those
        # two points this execution IS the owner, and the cron must already
        # be standing down.
        self._shepherd_in_loop = shepherd_in_loop
        # #231: run the tick concurrently so polling continues while it
        # runs. Awaiting it inline stalled merge detection for the tick's
        # whole duration — up to the 2 h SDK_STEP_TIMEOUT when the shepherd
        # spawns a follow-up implementation, against a 30-min poll
        # interval. Its own marker, because it changes the ORDER of
        # commands in history: executions that already recorded a
        # sequential tick must keep replaying one.
        concurrent_ticks = shepherd_in_loop and workflow.patched("concurrent-shepherd-tick")
        tick_task: asyncio.Task[None] | None = None
        poll_index = 0
        shepherd_ticks = 0
        try:
            while workflow.now() < deadline:
                try:
                    state: PRState = await workflow.execute_activity(
                        get_pr_state,
                        args=[service, slug],
                        start_to_close_timeout=PR_STATE_TIMEOUT,
                        retry_policy=PR_STATE_RETRY_POLICY,
                    )
                except ActivityError as exc:
                    # A read outage longer than the activity's retries must not
                    # abort a 14-day watch — ride KNOWN-transient failures out
                    # and poll again next interval (the deadline still bounds
                    # the loop). But only those: get_pr_state wraps every
                    # expected read failure in ProposalListingError, and an
                    # activity timeout is transport by definition, so anything
                    # else here is an unexpected bug in the activity — retrying
                    # that for 14 days would silently mask the defect (agy P2
                    # round 3). End the watch with the last observed state
                    # instead; implement already succeeded, so the loop's
                    # outcome must still not become a workflow failure.
                    cause = exc.cause
                    transient = isinstance(cause, TemporalTimeoutError) or (
                        isinstance(cause, ApplicationError)
                        and cause.type == "ProposalListingError"
                    )
                    if not transient:
                        workflow.logger.warning(
                            "get_pr_state failed with a non-transient error for "
                            "%s/%s — ending the merge watch with the last "
                            "observed state: %r",
                            service,
                            slug,
                            cause,
                        )
                        return last
                    workflow.logger.warning(
                        "get_pr_state failed after retries for %s/%s — retrying "
                        "next poll interval",
                        service,
                        slug,
                    )
                    await workflow.sleep(MERGE_POLL_INTERVAL)
                    continue
                if state.found:
                    last = state
                    polls_without_pr = 0
                    if state.state in ("MERGED", "CLOSED"):
                        return state
                    # Counted only on a successful read, so a transient
                    # get_pr_state failure delays the next tick instead of
                    # consuming its boundary and dropping it for ~4 h.
                    poll_index += 1
                    if (
                        shepherd_in_loop
                        and poll_index % SHEPHERD_TICK_EVERY_POLLS == 0
                        and shepherd_ticks < SHEPHERD_TICKS_MAX
                    ):
                        # The tick is the same one-shot the cron ran, scoped to
                        # exactly this proposal (service+slug → run_shepherd's
                        # targeted mode, which bypasses the ownership filter).
                        # A failed tick is logged, not fatal: the watch itself
                        # is the durable part, and the next tick retries.
                        if not concurrent_ticks:
                            # Legacy sequential path. Histories recorded before
                            # the concurrent-shepherd-tick marker interleave the
                            # tick's commands with the poll's in exactly this
                            # order; running them as a task instead would be a
                            # command mismatch on replay.
                            shepherd_ticks += 1
                            await self._shepherd_tick(service, slug)
                        elif tick_task is not None and not tick_task.done():
                            # Never two shepherds on one proposal: they would
                            # race on the same .status.yaml, and the tick cap
                            # accounting assumes one at a time. A boundary that
                            # lands on a still-running tick is skipped, not
                            # queued — and does not consume the budget.
                            workflow.logger.info(
                                "in-loop shepherd tick still running for %s/%s — "
                                "skipping this tick boundary",
                                service,
                                slug,
                            )
                        else:
                            # Drain the previous, already-finished tick
                            # before dropping the reference: after this
                            # rebind nothing else can retrieve its
                            # exception, and only the final task is
                            # guaranteed to reach _settle_tick.
                            if tick_task is not None:
                                _drain_tick(tick_task, service, slug)
                            shepherd_ticks += 1
                            tick_task = asyncio.create_task(self._shepherd_tick(service, slug))
                else:
                    polls_without_pr += 1
                    if last is None and state.number is not None:
                        # A PR link IS recorded but cannot be resolved right now
                        # (repo/PR deleted, token lost access, wrong-repo link).
                        # Preserve the reference in the result — it is the only
                        # diagnostic pointer an operator gets. Only when nothing
                        # better exists: a previously RESOLVED state must not be
                        # downgraded to a found=False reference by a later 404.
                        last = state
                    # Give up after GRACE consecutive unresolvable polls — this
                    # covers the link never appearing, a recorded PR that stays
                    # unresolvable, AND a status file deleted after the PR was
                    # once found (agy P3's zombie loop). The counter resets on
                    # every successful resolve, so one transient blip never
                    # ends the watch.
                    if polls_without_pr >= PR_LOOKUP_GRACE_POLLS:
                        workflow.logger.warning(
                            "merge watch for %s/%s giving up after %d consecutive "
                            "polls without a resolvable PR (last=%s)",
                            service,
                            slug,
                            polls_without_pr,
                            "none" if last is None else (last.pr_url or "unresolved"),
                        )
                        return last
                await workflow.sleep(MERGE_POLL_INTERVAL)
        finally:
            await self._settle_tick(tick_task, service, slug)
        return last
