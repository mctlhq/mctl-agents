"""Execution-evidence producer: one sealed `ExecutionEvidence` envelope per
governed runner invocation, posted to mctl-api's Tier B store
(mctlhq/mctl-agents#544, parent #199, ADR 018 "Producer call points").

Where it runs. The investigator (`run_issue_investigator.investigate`), the
implementer (`run_implementer.implement_one` / `review_feedback_one`) and the
shepherd (`run_shepherd.main`, once per processed proposal) wrap their run in
`run(stage)`. The wrapper is a `finally` path: it seals and posts for
success, failure and refusal alike, and for an exception escaping the run
(outcome `failed`, reason `unhandled-exception` / `system-exit`), which it
then re-raises unchanged.

What goes in. Only canonical execution records, by reference (ADR 018
sec. 1, acceptance criterion 1 of #199 as reworded on 2026-10-04):

- `execution`: the runner's ADR 011 `ExecutionContext.context_id` (`ex-`) and,
  for the investigator, the work-item store's `we_` + work item id once the
  work-item layer resolved one. `trace_id` only when it is known: the live
  OpenTelemetry trace when tracing (#195) is on, else a control-plane-minted
  context's own trace id. Tracing is enrichment, never a dependency.
- `policy_decisions` and `approvals`: every `policy_checkpoint` decision this
  run made, captured in-process from `policy_checkpoint.emit` (the one place
  every decision is recorded). An approval-flow decision's `aar_` receipt
  becomes an `ApprovalRef` bound to the action digest (the intent hash).
- `snapshot_refs` and `execution_request`: the investigator's sealed
  `ContextSnapshot` (local `cs-` id and, when the store kept it, the `cs_`
  id) and the `xr_` request it fulfils, read by the producer itself on the
  run's own work item (`context_assembly.py` is deliberately not touched:
  it is part of the context strategies' pinned `implementationHash`).
- `usage`: the usage-ledger join keys `(session_id, model_key)` of the run's
  SDK session, observed where the ledger itself observes it.
- `artifacts`: the investigator's published proposal triplet, by sha256.
- `outcome`: the run's own typed result, mapped to `OUTCOME_CODES`.

Unknown is not absence. A source that was expected but could not be read
becomes `Gap(code="observation_failed", required=True)` and the envelope is
`INCOMPLETE`; it is never an empty block. Observed-and-empty is an absent
block plus a non-required `not_applicable` gap. A block the run was
supposed to produce and did not is `not_produced`.

ADR 018 Amendment 2 blocks (`versions`, `subject`, `tool_calls`,
`provenance`) are emitted only when `MCTL_EVIDENCE_AMENDMENT_2` is truthy.
It defaults to off because mctl-api answers `400 evidence_invalid` for those
keys until its Tier B follow-up (ADR 018, "Tier B follow-up — checklist") is
released. Flip it on in the CWFTs only after that release; until then every
envelope has the pre-amendment shape, which stays valid.

Delivery. `POST /api/v1/evidence/records` with `{"envelope_b64": ...}`, the
base64 of the envelope's canonical JSON, and `Authorization: Bearer
$MCTL_EVIDENCE_WRITER_TOKEN` (the `service:mctl-agents-evidence` principal,
which can do nothing else; never the admin `MCTL_TOKEN`).

- 201 = stored, 200 = replay. mctl-api derives the row id from the
  envelope's content (`ev-` + `content_hash`, re-derived server-side) and
  answers an identical re-post, or a re-seal that only moved `created_at`,
  with the first stored row. A retry after a lost answer is therefore safe:
  every attempt sends the same bytes.
- 5xx, 429 and transport errors are retried, at most `ATTEMPTS` times,
  then reported as undelivered. Undelivered evidence is a loss to report,
  never "no evidence".
- Any other 4xx (400 invalid, 401/403, 409 divergence) is a refusal: a
  producer or credential bug, logged as a warning, never retried.

Never fatal. Evidence is bookkeeping about a run, not part of it. Every
build, seal or delivery failure is logged, counted (`stats()`) and
swallowed; the run's result and exit code are unchanged. No token means the
producer is off: one log line per process, nothing built, nothing posted.
The token is blanked out of every SDK session's environment
(`orchestrator.options._scrubbed`), like the usage-writer token.
"""
from __future__ import annotations

import base64
import collections
import contextlib
import json
import logging
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from orchestrator import execution_evidence as ee
from orchestrator.context_snapshot import canonical_json, hash_bytes

logger = logging.getLogger(__name__)

TOKEN_ENV = "MCTL_EVIDENCE_WRITER_TOKEN"  # noqa: S105 — an env var name, not a credential
AMENDMENT_2_ENV = "MCTL_EVIDENCE_AMENDMENT_2"
BASE_URL_ENV = "MCTL_API_BASE_URL"
DEFAULT_BASE_URL = "https://api.mctl.ai"
INGEST_PATH = "/api/v1/evidence/records"

# Bounded: at most ATTEMPTS posts of REQUEST_TIMEOUT_SECONDS each, with the
# delays between them, so a dead store costs a run's tail under 20 s.
REQUEST_TIMEOUT_SECONDS = 5.0
ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (1.0, 2.0)
# The implementer's PR head is read once at seal time (Amendment 2 only).
PR_HEAD_READ_TIMEOUT_SECONDS = 20.0

STAGE_INVESTIGATOR = "investigator"
STAGE_IMPLEMENTER = "implementer"
STAGE_SHEPHERD = "shepherd"
STAGES = frozenset({STAGE_INVESTIGATOR, STAGE_IMPLEMENTER, STAGE_SHEPHERD})

# Delivery results, also the keys of `stats()`.
CREATED = "created"
REPLAYED = "replayed"
REFUSED = "refused"
UNDELIVERED = "undelivered"
DISABLED = "disabled"
DISCARDED = "discarded"
BUILD_FAILED = "build_failed"

EVIDENCE_LOG_PREFIX = "EXECUTION_EVIDENCE"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_GIT_SHA_RE = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_RUNTIME_ID_RE = re.compile(r"ex-[0-9a-f]{16}")
_TRACE_ID_RE = re.compile(r"[0-9a-f]{32}")
_REPO_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}")
_PR_URL_RE = re.compile(r"https://github\.com/([A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100})/pull/([1-9][0-9]{0,9})")
_TOOL_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
_SLUG_INVALID = re.compile(r"[^a-z0-9._-]+")
_VERSION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:+-]{0,127}")

# policy_checkpoint decision code -> the aar_ receipt's state after that
# decision. `approved` means this decision spent the receipt (decide()
# consumes at decision time), so the receipt is `consumed` now.
_APPROVAL_STATE_BY_CODE = {
    "approved": "consumed",
    "approval_pending": "pending",
    "approval_denied": "denied",
    "approval_expired": "expired",
    "approval_consumed": "consumed",
}
# An approval lookup that could not answer: the receipt's state is unknown.
_APPROVAL_UNKNOWN_CODES = frozenset({"approval_lookup_error"})

Post = Callable[[str, dict[str, Any], dict[str, str]], httpx.Response]
PrHeadReader = Callable[[str, int], str]
RequestReader = Callable[[str, str], Any]


def _utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def slug(value: str, *, fallback: str = "unspecified") -> str:
    """`value` as an `Outcome.reason_code` slug (`[a-z0-9][a-z0-9._-]*`,
    at most 128 chars), or `fallback` when nothing usable is left."""
    text = _SLUG_INVALID.sub("-", (value or "").strip().lower()).strip("-._")
    return text[: ee.MAX_SLUG_LENGTH].rstrip("-._") or fallback


def _version_token(value: Any) -> str:
    return value if isinstance(value, str) and _VERSION_RE.fullmatch(value) else ""


def amendment_2_enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(AMENDMENT_2_ENV, "").strip().lower() in _TRUTHY


def agent_env_without_evidence_token(env: Mapping[str, str]) -> dict[str, str]:
    """`env` with the evidence-writer token blanked, for an SDK session's
    env: only this process's producer may hold it, never the CLI child or
    anything the model runs through Bash."""
    return {**env, TOKEN_ENV: ""}


@dataclass(frozen=True)
class _SeenDecision:
    action_kind: str
    operation: str
    action_digest: str
    verdict: str
    code: str
    policy_version: str
    rule_id: str
    approval_ref: str


class RunEvidence:
    """What one governed run observed about itself, collected while it runs.

    Every `note_*` method is safe to call with anything: a value outside its
    block's shape is not stored and does not raise (the producer must never
    become the reason a run fails). A source that was expected and could not
    be read is recorded with `note_unobserved`, never by leaving a block
    empty.
    """

    def __init__(self, stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"stage must be one of {sorted(STAGES)}, got {stage!r}")
        self.stage = stage
        self.discarded = False
        self.runtime_execution_id = ""
        self.runtime_asserted_by = ""
        self.execution_id = ""
        self.work_item_id = ""
        self.trace_id = ""
        self.outcome: tuple[str, str] | None = None
        self.decisions: list[_SeenDecision] = []
        self.tool_results: dict[str, str] = {}
        self.snapshot_refs: list[ee.SnapshotRef] = []
        self.execution_request: ee.ExecutionRequestRef | None = None
        self.execution_request_id = ""
        self.usage: ee.UsageRef | None = None
        self.usage_unrecorded = False
        self.artifacts: list[ee.ArtifactRef] = []
        self.gaps: list[ee.Gap] = []
        self.expected: set[str] = set()
        self.versions: ee.VersionPins | None = None
        self.subject: ee.SubjectRef | None = None
        self.subject_observed_at = ""
        # (repository, number) of a PR whose head the producer reads itself
        # at seal time (Amendment 2 only).
        self.pending_pr: tuple[str, int] | None = None
        self._lock = threading.Lock()

    # -- identity ---------------------------------------------------------
    def note_runtime_context(self, context: Any) -> None:
        """The run's ADR 011 `ExecutionContext`: its `ex-` id, and its trace
        id only when the control plane minted it (a local mint's trace id
        names no trace anyone else knows)."""
        context_id = getattr(context, "context_id", "")
        if isinstance(context_id, str) and _RUNTIME_ID_RE.fullmatch(context_id):
            self.runtime_execution_id = context_id
        assertions = getattr(context, "assertions", None)
        self.runtime_asserted_by = str(getattr(assertions, "asserted_by", "") or "")
        trace_id = getattr(context, "trace_id", "")
        if (
            not self.trace_id
            and self.runtime_asserted_by == "control-plane"
            and isinstance(trace_id, str)
            and _TRACE_ID_RE.fullmatch(trace_id)
        ):
            self.trace_id = trace_id
        self._note_live_trace()

    def _note_live_trace(self) -> None:
        """The live #195 trace wins over the context's: it is the trace the
        run's spans actually landed in."""
        try:
            from orchestrator import tracing

            ids = tracing.current_trace_ids()
        except Exception:  # noqa: BLE001 — tracing is optional enrichment
            return
        if ids and _TRACE_ID_RE.fullmatch(ids[0]):
            self.trace_id = ids[0]

    def note_work_execution(self, execution_id: str, work_item_id: str) -> None:
        if isinstance(execution_id, str) and execution_id.startswith(ee.EXECUTION_ID_PREFIX):
            self.execution_id = execution_id
            self.work_item_id = work_item_id if isinstance(work_item_id, str) else ""

    # -- outcome ----------------------------------------------------------
    def set_outcome(self, code: str, reason: str = "") -> None:
        if code not in ee.OUTCOME_CODES:
            code, reason = "failed", f"unknown-outcome-{code}"
        self.outcome = (code, slug(reason, fallback="") if reason else "")

    def discard(self) -> None:
        """This invocation governed nothing (a dry run, a no-op tick):
        produce no envelope for it."""
        self.discarded = True

    # -- canonical records ------------------------------------------------
    def expect(self, block: str) -> None:
        if block in ee.BLOCK_NAMES:
            self.expected.add(block)

    def note_unobserved(self, block: str, code: str = "observation_failed") -> None:
        """`block` was expected and could not be observed (or, with
        `code="not_produced"`, the run did not produce it)."""
        if block in ee.BLOCK_NAMES and code in ee.GAP_CODES:
            self.expected.add(block)
            self._add_gap(ee.Gap(block=block, code=code, required=True))

    def _add_gap(self, gap: ee.Gap) -> None:
        with self._lock:
            if gap not in self.gaps:
                self.gaps.append(gap)

    def record_decision(self, request: Any, decision: Any) -> None:
        seen = _SeenDecision(
            action_kind=str(getattr(request, "action_kind", "") or ""),
            operation=str(getattr(request, "operation", "") or ""),
            action_digest=str(getattr(decision, "action_digest", "") or ""),
            verdict=str(getattr(decision, "verdict", "") or ""),
            code=str(getattr(decision, "code", "") or ""),
            policy_version=str(getattr(decision, "policy_version", "") or ""),
            rule_id=str(getattr(decision, "rule_id", "") or ""),
            approval_ref=str(getattr(decision, "approval_ref", "") or ""),
        )
        with self._lock:
            self.decisions.append(seen)

    def record_tool_result(self, action_digest: str, succeeded: bool) -> None:
        if isinstance(action_digest, str) and action_digest:
            with self._lock:
                self.tool_results[action_digest] = "succeeded" if succeeded else "failed"

    def note_snapshot(self, snapshot: Any, store_ref: Any = None) -> None:
        """A sealed `ContextSnapshot` (local `cs-` id) and, when the store
        kept it, the store's `cs_` id with its content hash."""
        refs: list[ee.SnapshotRef] = []
        local_id = getattr(snapshot, "snapshot_id", "")
        local_hash = getattr(snapshot, "content_hash", "")
        if _snapshot_ref_ok(local_id, local_hash):
            refs.append(ee.SnapshotRef(snapshot_id=local_id, content_hash=local_hash))
        if store_ref is not None:
            store_id = getattr(store_ref, "store_snapshot_id", "")
            store_hash = getattr(store_ref, "store_content_hash", "")
            if _snapshot_ref_ok(store_id, store_hash):
                refs.append(ee.SnapshotRef(snapshot_id=store_id, content_hash=store_hash))
        with self._lock:
            for ref in refs:
                if ref not in self.snapshot_refs:
                    self.snapshot_refs.append(ref)

    def note_versions(self, correlation: Any) -> None:
        """The release pins off a snapshot's `ExecutionCorrelation` (the
        ADR 007 `ExecutionPlan` pins, or the legacy definition hash)."""
        definition_hash = getattr(correlation, "definition_content_hash", "")
        agent = getattr(correlation, "agent", "")
        if not (isinstance(definition_hash, str) and _SHA256_RE.fullmatch(definition_hash)):
            return
        if not isinstance(agent, str) or not agent:
            return
        profile_hash = getattr(correlation, "profile_content_hash", "") or ""
        revision = getattr(correlation, "release_revision", None)
        self.versions = ee.VersionPins(
            agent=slug(agent, fallback=""),
            environment=slug(str(getattr(correlation, "environment", "") or ""), fallback=""),
            definition_version=_version_token(getattr(correlation, "definition_version", "")),
            definition_content_hash=definition_hash,
            profile_version=_version_token(getattr(correlation, "profile_version", "")),
            profile_content_hash=profile_hash if _SHA256_RE.fullmatch(str(profile_hash)) else "",
            release_revision=revision if isinstance(revision, int) and not isinstance(revision, bool) else None,
        )

    def expect_execution_request(self, request_id: str | None) -> None:
        if isinstance(request_id, str) and request_id.startswith(ee.REQUEST_ID_PREFIX):
            self.execution_request_id = request_id
            self.expected.add("execution_request")

    def note_execution_request_answer(self, request_id: str | None, answer: Any) -> None:
        """One read of the `xr_` request. A found request with a known kind
        and state is the reference; anything else is an unknown, unless an
        earlier read of the same request already found it."""
        if not isinstance(request_id, str) or not request_id.startswith(ee.REQUEST_ID_PREFIX):
            return
        self.expect_execution_request(request_id)
        request = getattr(answer, "request", None)
        kind = str(getattr(request, "kind", "") or "")
        state = str(getattr(request, "state", "") or "")
        if (
            getattr(answer, "verdict", "") == "request-found"
            and getattr(request, "request_id", "") == request_id
            and (not self.work_item_id or getattr(request, "work_item_id", "") == self.work_item_id)
            and kind in ee.EXECUTION_REQUEST_KINDS
            and state in ee.EXECUTION_REQUEST_STATES
        ):
            self.execution_request = ee.ExecutionRequestRef(request_id=request_id, kind=kind, state=state)

    def note_usage(self, session_id: str, model_keys: list[str], *, recorded: bool) -> None:
        """The run's SDK session, as the usage ledger saw it. The ledger's
        rows are keyed `(session_id, result_uuid, model_key)`; the reference
        names the session and its first model, so it joins every row of it.
        `recorded=False`: the ledger is off in this process, so those rows
        do not exist."""
        if not isinstance(session_id, str) or not session_id.strip():
            return
        self.expected.add("usage")
        if not recorded:
            self.usage_unrecorded = True
            return
        if self.usage is not None:
            return
        keys = [k for k in model_keys if isinstance(k, str) and k]
        if not keys:
            return
        stage = self.stage if self.stage in ee.USAGE_DEVLOOP_STAGES else None
        self.usage = ee.UsageRef(session_id=session_id.strip(), model_key=keys[0], devloop_stage=stage)

    def note_artifact_file(self, path: Path, kind: str) -> None:
        """A generated file, by name and sha256 of its bytes. A missing file
        was not produced; an unreadable one could not be observed."""
        self.expected.add("artifacts")
        try:
            if not path.is_file():
                self._add_gap(ee.Gap(block="artifacts", code="not_produced", required=True))
                return
            digest = hash_bytes(path.read_bytes())
        except OSError:
            self._add_gap(ee.Gap(block="artifacts", code="observation_failed", required=True))
            return
        ref = ee.ArtifactRef(name=path.name, kind=slug(kind, fallback="file"), content_hash=digest)
        with self._lock:
            self.artifacts.append(ref)

    # -- subject (Amendment 2) -------------------------------------------
    def note_subject_issue(self, repository: str, number: Any) -> None:
        if not (isinstance(repository, str) and _REPO_RE.fullmatch(repository)):
            return
        if not str(number).isdigit() or int(number) <= 0:
            return
        self.subject = ee.SubjectRef(kind="issue", repository=repository, ref=str(int(number)), revision="")
        self.subject_observed_at = _utc_now_iso()
        self.pending_pr = None

    def note_subject_pr(self, repository: str, number: Any, head_sha: str | None = None) -> None:
        """The PR this run acted on. With `head_sha`, the revision this run
        itself read; without, the producer reads the head at seal time."""
        if not (isinstance(repository, str) and _REPO_RE.fullmatch(repository)):
            return
        if not str(number).isdigit() or int(number) <= 0:
            return
        if isinstance(head_sha, str) and _GIT_SHA_RE.fullmatch(head_sha):
            self.subject = ee.SubjectRef(
                kind="pull_request", repository=repository, ref=str(int(number)), revision=head_sha
            )
            self.subject_observed_at = _utc_now_iso()
            self.pending_pr = None
            return
        self.subject = None
        self.pending_pr = (repository, int(number))

    def note_subject_pr_url(self, url: str | None) -> None:
        match = _PR_URL_RE.fullmatch((url or "").strip())
        if match:
            self.note_subject_pr(match.group(1), match.group(2))


# ---------------------------------------------------------------------------
# The active run. A stack, process-wide: decisions are emitted from SDK hook
# callbacks and helper threads, which a contextvar would not reach. Outside
# every run() (the Temporal worker, a poller) nothing is collected at all.
# ---------------------------------------------------------------------------
_ACTIVE: list[RunEvidence] = []
_ACTIVE_LOCK = threading.Lock()


def current() -> RunEvidence | None:
    with _ACTIVE_LOCK:
        return _ACTIVE[-1] if _ACTIVE else None


def _with_current(action: Callable[[RunEvidence], None]) -> None:
    """Apply `action` to the active run, if any. Never raises."""
    try:
        evidence = current()
        if evidence is not None:
            action(evidence)
    except Exception as exc:  # noqa: BLE001 — collection must never break the run it describes
        logger.warning("execution evidence: could not record an observation (%s: %s)", type(exc).__name__, exc)


def record_decision(request: Any, decision: Any) -> None:
    """Called by `policy_checkpoint.emit` for every decision."""
    _with_current(lambda run: run.record_decision(request, decision))


def record_tool_result(action_digest: str, succeeded: bool) -> None:
    """Called by `policy_checkpoint.enforce` once a permitted side effect
    returned or raised."""
    _with_current(lambda run: run.record_tool_result(action_digest, succeeded))


def note_usage(session_id: str, model_keys: list[str], *, recorded: bool) -> None:
    """Called by `usage_ledger.UsageRecorder.observe` for each ResultMessage."""
    _with_current(lambda run: run.note_usage(session_id, model_keys, recorded=recorded))


def note(method: str, *args: Any, **kwargs: Any) -> None:
    """`current().<method>(*args, **kwargs)` when a run is active; never
    raises. The call shape the runners use, so an annotation costs one line
    and no `None` check."""
    _with_current(lambda run: getattr(run, method)(*args, **kwargs))


@contextlib.contextmanager
def run(stage: str, **post_options: Any) -> Iterator[RunEvidence]:
    """Collect one governed run's evidence; seal and post it on the way out,
    however the run ends. An escaping exception is recorded as the outcome
    (unless the run set one) and re-raised unchanged."""
    evidence = RunEvidence(stage)
    with _ACTIVE_LOCK:
        _ACTIVE.append(evidence)
    raised: BaseException | None = None
    try:
        yield evidence
    except BaseException as exc:
        raised = exc
        raise
    finally:
        with _ACTIVE_LOCK:
            if evidence in _ACTIVE:
                _ACTIVE.remove(evidence)
        if raised is not None and evidence.outcome is None:
            _outcome_from_exception(evidence, raised)
        produce(evidence, **post_options)


def _outcome_from_exception(evidence: RunEvidence, exc: BaseException) -> None:
    if isinstance(exc, SystemExit):
        if exc.code in (0, None):
            evidence.set_outcome("succeeded", "system-exit-0")
        else:
            evidence.set_outcome("failed", "system-exit")
    elif isinstance(exc, KeyboardInterrupt):
        evidence.set_outcome("abandoned", "interrupted")
    else:
        evidence.set_outcome("failed", "unhandled-exception")


# ---------------------------------------------------------------------------
# Counters: what this process produced, for the log and for tests.
# ---------------------------------------------------------------------------
_COUNTS: collections.Counter[str] = collections.Counter()
_DISABLED_LOGGED = False


def stats() -> dict[str, int]:
    return dict(_COUNTS)


def _reset_for_tests() -> None:
    global _DISABLED_LOGGED
    _COUNTS.clear()
    _DISABLED_LOGGED = False
    with _ACTIVE_LOCK:
        _ACTIVE.clear()


def _count(result: str) -> None:
    _COUNTS[result] += 1


# ---------------------------------------------------------------------------
# Build: RunEvidence -> sealed ExecutionEvidence.
# ---------------------------------------------------------------------------
def _snapshot_ref_ok(snapshot_id: Any, content_hash: Any) -> bool:
    return (
        isinstance(snapshot_id, str)
        and snapshot_id.startswith(ee.SNAPSHOT_ID_PREFIXES)
        and isinstance(content_hash, str)
        and bool(_SHA256_RE.fullmatch(content_hash))
    )


def read_pr_head_sha(repository: str, number: int) -> str:
    """The PR's current head SHA, read from GitHub by the producer itself.
    Raises on any failure: the caller turns that into an unknown."""
    proc = subprocess.run(  # noqa: S603 — fixed argv, validated repository and number
        ["gh", "api", f"repos/{repository}/pulls/{int(number)}", "--jq", ".head.sha"],  # noqa: S607
        capture_output=True,
        text=True,
        timeout=PR_HEAD_READ_TIMEOUT_SECONDS,
        check=False,
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not _GIT_SHA_RE.fullmatch(sha):
        raise RuntimeError(f"gh api pulls/{number} answered rc={proc.returncode}")
    return sha


def read_execution_request(work_item_id: str, request_id: str) -> Any:
    """One read of the `xr_` request from the work-item store."""
    from orchestrator.work_context.client import WorkItemClient

    return WorkItemClient().execution_request(work_item_id, request_id)


def _runtime_from_environment(evidence: RunEvidence) -> None:
    """A run that ended before it loaded its own ExecutionContext (an early
    refusal) still has one when the control plane wrote it: load that one.
    A local mint is never used here — its id is fresh per load, so it
    would name an execution no other record carries."""
    from orchestrator.execution_identity import MCTL_EXECUTION_CONTEXT_FILE_ENV, load_from_environment

    if not os.environ.get(MCTL_EXECUTION_CONTEXT_FILE_ENV, "").strip():
        return
    try:
        context = load_from_environment(executor_type=evidence.stage, workflow_type="evidence", agent=evidence.stage)
    except Exception:  # noqa: BLE001 — an unreadable context is an unknown, recorded as a gap below
        return
    if getattr(getattr(context, "assertions", None), "asserted_by", "") == "control-plane":
        evidence.note_runtime_context(context)


def _policy_refs(evidence: RunEvidence) -> tuple[list[ee.PolicyDecisionRef], list[ee.ApprovalRef], list[ee.Gap]]:
    decisions: list[ee.PolicyDecisionRef] = []
    approvals: dict[str, ee.ApprovalRef] = {}
    gaps: list[ee.Gap] = []
    for seen in evidence.decisions:
        digest = seen.action_digest if _SHA256_RE.fullmatch(seen.action_digest) else ""
        approval_ref = seen.approval_ref if seen.approval_ref.startswith(ee.APPROVAL_ID_PREFIX) else ""
        decisions.append(ee.PolicyDecisionRef(
            action_digest=digest,
            verdict=seen.verdict if seen.verdict in ee.VERDICTS else "",
            code=seen.code,
            policy_version=seen.policy_version,
            rule_id=seen.rule_id,
            approval_ref=approval_ref,
        ))
        if not approval_ref:
            continue
        state = _APPROVAL_STATE_BY_CODE.get(seen.code)
        if state is not None and digest:
            approvals[approval_ref] = ee.ApprovalRef(approval_id=approval_ref, intent_hash=digest, state=state)
        elif seen.code in _APPROVAL_UNKNOWN_CODES:
            gaps.append(ee.Gap(block="approvals", code="observation_failed", required=True))
    return decisions, list(approvals.values()), gaps


def _tool_calls(evidence: RunEvidence) -> tuple[list[ee.ToolCallRef], list[ee.Gap]]:
    calls: list[ee.ToolCallRef] = []
    for seen in evidence.decisions:
        if seen.action_kind not in ee.TOOL_CALL_KINDS or not _SHA256_RE.fullmatch(seen.action_digest):
            continue
        if seen.code not in ("allowed", "approved"):
            status = "refused"
        else:
            # A permitted call whose side effect this process did not see
            # finish (checkpoint() + require() call sites) is `unknown`,
            # never assumed to have succeeded.
            status = evidence.tool_results.get(seen.action_digest, "unknown")
        calls.append(ee.ToolCallRef(
            kind=seen.action_kind,
            name=seen.operation if _TOOL_NAME_RE.fullmatch(seen.operation) else "",
            action_digest=seen.action_digest,
            status=status,
        ))
    if len(calls) > ee.MAX_TOOL_CALLS:
        return calls[: ee.MAX_TOOL_CALLS], [ee.Gap(block="tool_calls", code="observation_failed", required=True)]
    return calls, []


def build(
    evidence: RunEvidence,
    *,
    amendment_2: bool,
    created_at: str | None = None,
    read_pr_head: PrHeadReader | None = None,
    read_request: RequestReader | None = None,
) -> ee.ExecutionEvidence:
    """Seal `evidence`. Raises `ExecutionEvidenceError` only when not even
    the degraded envelope (execution, outcome and gaps) can be sealed."""
    created_at = created_at or _utc_now_iso()
    if not evidence.runtime_execution_id:
        _runtime_from_environment(evidence)
    gaps: list[ee.Gap] = list(evidence.gaps)
    flags: dict[str, bool] = {"policy_decisions": False}

    execution = ee.ExecutionJoin(
        execution_id=evidence.execution_id,
        work_item_id=evidence.work_item_id if evidence.execution_id else "",
        trace_id=evidence.trace_id,
        runtime_execution_id=evidence.runtime_execution_id,
    )
    if not (execution.execution_id or execution.runtime_execution_id):
        gaps.append(ee.Gap(block="execution", code="observation_failed", required=True))

    code, reason = evidence.outcome or ("failed", "no-outcome-recorded")
    outcome = ee.Outcome(code=code, reason_code=reason)

    decisions, approvals, approval_gaps = _policy_refs(evidence)
    gaps.extend(approval_gaps)
    if decisions:
        flags["policy_decisions"] = True
    else:
        # Observed: this run made no checkpointed action at all.
        gaps.append(ee.Gap(block="policy_decisions", code="not_applicable", required=False))
    if approvals:
        flags["approvals"] = True

    execution_request = evidence.execution_request
    if evidence.execution_request_id and execution_request is None and evidence.work_item_id:
        # Never read during the run: read it once now, on the run's own
        # work item. A failed read stays an unknown.
        try:
            reader = read_request or read_execution_request
            evidence.note_execution_request_answer(
                evidence.execution_request_id, reader(evidence.work_item_id, evidence.execution_request_id)
            )
            execution_request = evidence.execution_request
        except Exception as exc:  # noqa: BLE001 — an unknown, recorded as a gap below
            logger.warning("execution evidence: could not read %s (%s)", evidence.execution_request_id, exc)

    usage = evidence.usage
    if evidence.usage_unrecorded and usage is None:
        gaps.append(ee.Gap(block="usage", code="store_unavailable", required=True))

    present = {
        "snapshot_refs": bool(evidence.snapshot_refs),
        "execution_request": execution_request is not None,
        "usage": usage is not None,
        "artifacts": bool(evidence.artifacts),
    }
    for block, has in present.items():
        if block in evidence.expected:
            flags[block] = True
            if not has and not any(g.block == block for g in gaps):
                gaps.append(ee.Gap(block=block, code="observation_failed", required=True))

    versions: ee.VersionPins | None = None
    subject: ee.SubjectRef | None = None
    provenance: ee.Provenance | None = None
    tool_calls: list[ee.ToolCallRef] = []
    if amendment_2:
        versions = evidence.versions
        if versions is None:
            gaps.append(ee.Gap(block="versions", code="not_produced", required=False))
        subject, observed_at = evidence.subject, evidence.subject_observed_at
        if subject is None and evidence.pending_pr is not None:
            repository, number = evidence.pending_pr
            flags["subject"] = True
            try:
                sha = (read_pr_head or read_pr_head_sha)(repository, number)
                if not _GIT_SHA_RE.fullmatch(sha):
                    raise ValueError("not a full git SHA")
                subject = ee.SubjectRef(kind="pull_request", repository=repository, ref=str(number), revision=sha)
                observed_at = _utc_now_iso()
            except Exception as exc:  # noqa: BLE001 — could not observe the revision: an unknown
                logger.warning("execution evidence: could not read the head of %s#%s (%s)", repository, number, exc)
                gaps.append(ee.Gap(block="subject", code="observation_failed", required=True))
        if subject is not None:
            provenance = ee.Provenance(authority="observed", observed_at=observed_at or created_at)
        elif not any(g.block == "subject" for g in gaps):
            gaps.append(ee.Gap(block="subject", code="not_applicable", required=False))
        tool_calls, call_gaps = _tool_calls(evidence)
        gaps.extend(call_gaps)

    blocks: dict[str, Any] = {
        "policy_decisions": decisions,
        "snapshot_refs": list(evidence.snapshot_refs),
        "execution_request": execution_request,
        "usage": usage,
        "approvals": approvals,
        "artifacts": list(evidence.artifacts),
        "versions": versions,
        "subject": subject,
        "tool_calls": tool_calls,
        "provenance": provenance,
    }
    try:
        return ee.seal(
            execution=execution, outcome=outcome, created_at=created_at, gaps=_dedupe(gaps),
            requirements=ee.Requirements(**flags), **blocks,
        )
    except ee.ExecutionEvidenceError as exc:
        # One malformed reference must not cost the whole envelope: keep the
        # execution, the outcome and the gaps, and turn every block that was
        # going to be sent into an explicit unknown.
        logger.warning("execution evidence: sealing failed (%s); sealing a degraded envelope", exc)
        _count("degraded")
        degraded = [*gaps, *(
            ee.Gap(block=name, code="observation_failed", required=True)
            for name, value in blocks.items()
            if value
        )]
        return ee.seal(
            execution=execution, outcome=outcome, created_at=created_at, gaps=_dedupe(degraded),
            requirements=ee.Requirements(policy_decisions=False),
        )


def _dedupe(gaps: list[ee.Gap]) -> list[ee.Gap]:
    seen: list[ee.Gap] = []
    for gap in gaps:
        if gap not in seen:
            seen.append(gap)
    return seen


def envelope_bytes(evidence: ee.ExecutionEvidence) -> bytes:
    """The exact bytes posted: the envelope's canonical JSON (the one
    serialization rule this repository declares)."""
    return canonical_json(evidence.to_dict())


# ---------------------------------------------------------------------------
# Delivery.
# ---------------------------------------------------------------------------
def _default_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    return httpx.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)


def _error_code(resp: httpx.Response) -> str:
    try:
        data = resp.json()
    except Exception:  # noqa: BLE001 — a non-JSON error body has no code
        return ""
    if isinstance(data, Mapping):
        code = data.get("code") or (data.get("error") if isinstance(data.get("error"), str) else "")
        return str(code or "")[:64]
    return ""


def deliver(
    raw: bytes,
    *,
    url: str,
    token: str,
    post: Post | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> tuple[str, int, str]:
    """POST `raw`; returns `(result, http_status, detail)`. Never raises.

    Every attempt sends the same bytes, so a retry after a lost answer is a
    replay the store answers 200, never a second record."""
    body = {"envelope_b64": base64.b64encode(raw).decode("ascii")}
    headers = {"Authorization": f"Bearer {token}"}
    send = post or _default_post
    status, detail = 0, ""
    for attempt in range(1, ATTEMPTS + 1):
        try:
            resp = send(url, body, headers)
        except httpx.HTTPError as exc:
            status, detail = 0, type(exc).__name__
        except Exception as exc:  # noqa: BLE001 — a broken transport is undelivered, not a crash
            return UNDELIVERED, 0, type(exc).__name__
        else:
            status = resp.status_code
            if status == 201:
                return CREATED, status, ""
            if status == 200:
                return REPLAYED, status, ""
            detail = _error_code(resp)
            if status < 500 and status != 429:
                return REFUSED, status, detail
        if attempt < ATTEMPTS:
            sleep(RETRY_DELAYS_SECONDS[min(attempt - 1, len(RETRY_DELAYS_SECONDS) - 1)])
    return UNDELIVERED, status, detail


def _credentials(env: Mapping[str, str]) -> tuple[str, str, str]:
    """(token, ingest url, why off). A token is only ever sent over https."""
    token = env.get(TOKEN_ENV, "").strip()
    base_url = env.get(BASE_URL_ENV, "").strip() or DEFAULT_BASE_URL
    if not token:
        return "", "", f"{TOKEN_ENV} is not set"
    if not base_url.startswith("https://"):
        return "", "", f"{BASE_URL_ENV} is not https ({base_url!r})"
    return token, base_url.rstrip("/") + INGEST_PATH, ""


def _log_line(stage: str, result: str, evidence: ee.ExecutionEvidence | None, status: int, detail: str) -> None:
    line: dict[str, Any] = {"stage": stage, "delivery": result}
    if evidence is not None:
        line.update(evidence.to_log_dict())
    if status:
        line["http_status"] = status
    if detail:
        line["detail"] = detail
    print(f"{EVIDENCE_LOG_PREFIX} {json.dumps(line, sort_keys=True)}", flush=True)


def produce(
    evidence: RunEvidence,
    *,
    environ: Mapping[str, str] | None = None,
    post: Post | None = None,
    sleep: Callable[[float], None] = time.sleep,
    read_pr_head: PrHeadReader | None = None,
    read_request: RequestReader | None = None,
    created_at: str | None = None,
) -> str:
    """Seal and post one run's evidence. Returns the delivery result and
    NEVER raises: whatever happens here, the run's own outcome stands."""
    global _DISABLED_LOGGED
    try:
        if evidence.discarded:
            _count(DISCARDED)
            return DISCARDED
        env = os.environ if environ is None else environ
        token, url, off_reason = _credentials(env)
        if not token:
            _count(DISABLED)
            if not _DISABLED_LOGGED:
                _DISABLED_LOGGED = True
                logger.warning("execution evidence is off (%s): this process posts no evidence", off_reason)
            return DISABLED
        sealed = build(
            evidence,
            amendment_2=amendment_2_enabled(env),
            created_at=created_at,
            read_pr_head=read_pr_head,
            read_request=read_request,
        )
        result, status, detail = deliver(envelope_bytes(sealed), url=url, token=token, post=post, sleep=sleep)
    except Exception as exc:  # noqa: BLE001 — evidence must never break the run it describes
        _count(BUILD_FAILED)
        logger.warning("execution evidence could not be built (%s: %s)", type(exc).__name__, exc)
        try:
            _log_line(evidence.stage, BUILD_FAILED, None, 0, type(exc).__name__)
        except Exception:  # noqa: BLE001, S110 — logging is best effort too
            pass
        return BUILD_FAILED
    _count(result)
    if result == REFUSED:
        logger.warning(
            "execution evidence %s was refused (HTTP %s %s); %d refused so far in this process",
            sealed.evidence_id, status, detail, _COUNTS[REFUSED],
        )
    elif result == UNDELIVERED:
        logger.warning(
            "execution evidence %s was not delivered (%s); %d undelivered so far in this process",
            sealed.evidence_id, detail or f"HTTP {status}", _COUNTS[UNDELIVERED],
        )
    try:
        _log_line(evidence.stage, result, sealed, status, detail)
    except Exception:  # noqa: BLE001, S110 — logging is best effort
        pass
    return result
