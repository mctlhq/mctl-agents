"""review-worker: the producer of autonomous independent review receipts
(mctlhq/mctl-agents#986, ADR 025 "Producer").

One run reviews one subject (a proposal or a spike report) and ends in exactly
one outcome. Order, and no step runs after one refuses:

    authoring facts -> open worker request -> reviewer work item -> attach a
    new reviewer execution (service principal, directly) -> local
    independence check -> read-only reviewer session -> mint a `review`
    run token + submit -> seal ExecutionEvidence

The service credential and the run token meet only in this process. The
model session sees neither, and holds no GitHub credential.

The reviewer execution this run attaches is always advanced to a terminal
phase before the run ends, whatever the outcome: ``Succeeded`` only when the
receipt is stored, ``Failed`` otherwise (best effort, see ``_finish``).

Exit codes: 0 stored; 2 refused; 3 disabled (the route answers 501
`review_worker_ingest_disabled`); 4 unobserved. Anything but 0 means no
receipt was observed, and none of them is a PASS or "no receipt".
"""
from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from config.settings import AGENTS_DIR, SERVICE_AGENT_MODEL
from orchestrator import evidence_producer
from orchestrator import review_receipts as rr
from orchestrator.review_worker import contract as c
from orchestrator.review_worker import session, submitter
from orchestrator.review_worker.api import ReviewApi, reviewer_engine_run
from orchestrator.work_context import executions as wx

REVIEW_WORKER_DIR = AGENTS_DIR / "_review_worker"
LOG_PREFIX = "REVIEW_WORKER"

EXIT_BY_STATE = {c.STATE_STORED: 0, c.STATE_REFUSED: 2, c.STATE_DISABLED: 3, c.STATE_UNOBSERVED: 4}


@dataclass(frozen=True)
class RunArgs:
    subject_kind: str
    authoring_execution_id: str
    authoring_work_item_id: str
    author_actors: tuple[str, ...] = ()
    # proposal
    service: str = ""
    slug: str = ""
    gitops_commit: str = ""
    proposal_dir: Path | None = None
    approval_ref: str = ""
    # spike_report
    repository: str = ""
    report_root: str = ""
    report_path: str = ""
    source_sha: str = ""
    base_sha: str = ""
    head_sha: str = ""
    spike_dir: Path | None = None
    artifact_dir: Path = field(default_factory=lambda: Path("review-artifacts"))


def _subject_and_material(args: RunArgs) -> tuple[rr.ReviewSubject, session.Material]:
    if args.subject_kind == rr.SUBJECT_PROPOSAL:
        if args.proposal_dir is None:
            raise session.SessionError(rr.CODE_BAD_SUBJECT, "--proposal-dir is required")
        proposal = session.read_proposal_dir(args.proposal_dir)
        subject = rr.proposal_subject(
            service=args.service, slug=args.slug, gitops_commit=args.gitops_commit,
            files=proposal.files, approval_ref=args.approval_ref,
        )
        return subject, proposal
    if args.spike_dir is None:
        raise session.SessionError(rr.CODE_BAD_SUBJECT, "--spike-dir is required")
    spike = session.read_spike_dir(args.spike_dir, args.report_path)
    subject = rr.spike_subject(
        repository=args.repository, report_root=args.report_root, report_path=args.report_path,
        source_sha=args.source_sha, base_sha=args.base_sha, head_sha=args.head_sha, entries=spike.entries,
    )
    return subject, spike


async def sdk_runner(options: Any, prompt: str) -> str:
    """The reviewer session: one traced SDK query. Its final
    ``ResultMessage.result``, else the assistant text seen."""
    from claude_agent_sdk import AssistantMessage, ResultMessage, TextBlock, query

    from orchestrator import tracing

    texts: list[str] = []
    result = ""
    with tracing.agent_run(c.AGENT_NAME, options.model or "") as trace_run:
        async for message in query(prompt=prompt, options=options):
            trace_run.observe(message)
            if isinstance(message, AssistantMessage):
                texts.extend(b.text for b in message.content if isinstance(b, TextBlock))
            elif isinstance(message, ResultMessage) and isinstance(message.result, str):
                result = message.result
    return result or "\n".join(texts)


def _finish(api: ReviewApi, item_id: str, run: wx.EngineRun, outcome: c.Outcome | None) -> None:
    """Advance the reviewer execution this run attached to its terminal
    phase: ``Succeeded`` only for a stored receipt, ``Failed`` for every other
    end, an unexpected exception included (``outcome`` None). Best effort: a
    refusal or an unreachable store is logged and never changes the outcome.

    Unlike the investigator's ``_OwnExecution.finish`` this does NOT leave a
    failed execution ``Running`` when ``MCTL_ENGINE_FINAL_ATTEMPT=false``.
    The investigator's retry re-attaches the same engine run to the same work
    item and needs that execution open; a review-worker retry opens a fresh
    request with a fresh nonce, so it attaches to a different
    ``review:<request_id>`` item, and nothing would ever end this one."""
    phase = wx.PHASE_SUCCEEDED if outcome is not None and outcome.stored else wx.PHASE_FAILED
    why = api.finish_reviewer(item_id, run, phase)
    where = f"{run.engine}/{run.engine_ref} of {item_id}"
    if why:
        print(f"{LOG_PREFIX} warn: could not advance the reviewer execution {where} to {phase}: {why}", flush=True)
    else:
        print(f"{LOG_PREFIX} info: reviewer execution {where} -> {phase}", flush=True)


def _flow(
    args: RunArgs, api: ReviewApi, ev: evidence_producer.RunEvidence, runner: session.Runner,
) -> c.Outcome:
    facts = api.authoring_facts(args.authoring_execution_id)  # Stop: review_authoring_unresolved
    try:
        subject, material = _subject_and_material(args)
        request = rr.seal_request(
            subject=subject,
            mode="worker",
            author_actors=args.author_actors,
            nonce=secrets.token_urlsafe(32),  # memory only: never logged, stored or shown to the model
            authoring_execution_id=args.authoring_execution_id,
            authoring_attempt_ids=facts.attempt_ids,
            authoring_lineage_root=facts.lineage_root,
        )
    except (rr.ReviewError, session.SessionError) as exc:
        raise c.Stop(c.refused(exc.code)) from exc
    api.open_request(request)
    tenant = api.tenant_of(args.authoring_work_item_id)
    item_id = api.reviewer_work_item(request, tenant)
    engine_run, _why = wx.engine_ref_from_env()
    if engine_run is None:
        raise c.Stop(c.refused("engine_ref_missing", 0))
    reviewer_run = reviewer_engine_run(engine_run.engine_ref.split("#", 1)[0])
    execution_id = api.attach_reviewer(item_id, reviewer_run)
    # From here this run owns a Running execution: every end advances it.
    outcome: c.Outcome | None = None
    try:
        ev.note_work_execution(execution_id, item_id)
        outcome = _review_and_submit(args, api, ev, runner, request, material, execution_id)
        return outcome
    except c.Stop as stop:
        outcome = stop.outcome
        raise
    finally:
        _finish(api, item_id, reviewer_run, outcome)


def _review_and_submit(
    args: RunArgs, api: ReviewApi, ev: evidence_producer.RunEvidence, runner: session.Runner,
    request: rr.ReviewRequest, material: session.Material, execution_id: str,
) -> c.Outcome:
    provenance = api.reviewer_provenance(execution_id)
    not_independent = c.check_independent(request, provenance)
    if not_independent:
        raise c.Stop(c.refused(not_independent))
    with tempfile.TemporaryDirectory(prefix="review-worker-") as work:
        try:
            verdict = asyncio.run(session.run_review(
                request, material, model=SERVICE_AGENT_MODEL, agent_dir=REVIEW_WORKER_DIR,
                work_dir=Path(work), artifact_dir=args.artifact_dir, runner=runner,
            ))
        except session.SessionError as exc:
            raise c.Stop(c.refused(exc.code)) from exc
    findings = verdict.draft.findings_ref
    outcome = submitter.mint_and_submit(api, execution_id, request, verdict.draft)
    if outcome.stored:
        ev.note_artifact_ref(outcome.receipt_id, "review-receipt", outcome.content_hash)
        ev.note_artifact_ref(findings.name, findings.kind, findings.content_hash)
    return outcome


def _evidence_outcome(ev: evidence_producer.RunEvidence, outcome: c.Outcome) -> None:
    reason = outcome.code or outcome.state
    if outcome.stored:
        ev.set_outcome("succeeded", "receipt-stored")
    elif outcome.state == c.STATE_UNOBSERVED:
        ev.set_outcome("failed", reason)
    else:
        ev.set_outcome("refused", reason)


def run(
    args: RunArgs,
    *,
    api: ReviewApi | None = None,
    runner: session.Runner | None = None,
) -> c.Outcome:
    """Run the producer once. Never raises for an mctl-api answer: every one
    is an ``Outcome``."""
    owns_api = api is None
    api = api or ReviewApi()
    try:
        with evidence_producer.run(evidence_producer.STAGE_REVIEWER) as ev:
            try:
                outcome = _flow(args, api, ev, runner or sdk_runner)
            except c.Stop as stop:
                outcome = stop.outcome
            _evidence_outcome(ev, outcome)
    finally:
        if owns_api:
            api.close()
    line = {"state": outcome.state, "code": outcome.label, "http_status": outcome.http_status}
    if outcome.stored:
        line["receipt_id"] = outcome.receipt_id
    print(f"{LOG_PREFIX} {json.dumps(line, sort_keys=True)}", flush=True)
    return outcome


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orchestrator.run_review_worker", description=__doc__)
    p.add_argument("--subject-kind", required=True, choices=sorted(rr.SUBJECT_KINDS))
    p.add_argument("--authoring-execution-id", required=True)
    p.add_argument("--authoring-work-item-id", required=True,
                   help="tenant source only; the reviewer is never attached to this item")
    p.add_argument("--author-actor", action="append", default=[], dest="author_actors")
    p.add_argument("--artifact-dir", type=Path, default=Path("review-artifacts"))
    p.add_argument("--service")
    p.add_argument("--slug")
    p.add_argument("--gitops-commit")
    p.add_argument("--proposal-dir", type=Path)
    p.add_argument("--approval-ref", default="")
    p.add_argument("--repository")
    p.add_argument("--report-root")
    p.add_argument("--report-path")
    p.add_argument("--source-sha")
    p.add_argument("--base-sha")
    p.add_argument("--head-sha")
    p.add_argument("--spike-dir", type=Path)
    return p


def args_from_namespace(ns: argparse.Namespace) -> RunArgs:
    return RunArgs(
        subject_kind=ns.subject_kind,
        authoring_execution_id=ns.authoring_execution_id,
        authoring_work_item_id=ns.authoring_work_item_id,
        author_actors=tuple(ns.author_actors),
        service=ns.service or "", slug=ns.slug or "", gitops_commit=ns.gitops_commit or "",
        proposal_dir=ns.proposal_dir, approval_ref=ns.approval_ref,
        repository=ns.repository or "", report_root=ns.report_root or "", report_path=ns.report_path or "",
        source_sha=ns.source_sha or "", base_sha=ns.base_sha or "", head_sha=ns.head_sha or "",
        spike_dir=ns.spike_dir, artifact_dir=ns.artifact_dir,
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = args_from_namespace(_parser().parse_args(argv))
    from orchestrator.auth import ensure_auth_for_sdk

    ensure_auth_for_sdk()
    return EXIT_BY_STATE[run(args).state]


if __name__ == "__main__":
    sys.exit(main())
