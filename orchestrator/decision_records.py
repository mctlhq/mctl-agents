"""Durable decision records: the producer half of mctlhq/mctl-api#701
(mctl-api `docs/decision-records.md`, `internal/decisions`).

mctl-api keeps one immutable row per policy decision and an append-only list
of attempt events under it, so that a decision id alone reconstructs intent
-> policy -> authority/approval -> exact action -> every attempt -> terminal
result. This module writes them:

- every `policy_checkpoint` decision, from `policy_checkpoint.emit` (the one
  place every decision is recorded), as `POST /api/v1/decisions`;
- around a permitted side effect, `POST /api/v1/decisions/{id}/attempts`:
  `started` before it runs, then exactly one of `succeeded`, `failed` or
  `partial`. Each run of the side effect is a new attempt (`start()` again);
  mctl-api numbers attempts, never this process.

Off by default. `MCTL_DECISION_RECORDS=on` (or 1/true/yes) turns it on; any
other value, unset included, is off and nothing is built or posted.

What is sent. Canonical ids and digests only, never a payload:

- the action exactly as the approval store binds it (`action_approvals.
  intent_for`: execution id, `<kind>:<operation>`, args digest, rule, policy
  version), with the target as `sha256(target)` and the checkpoint's own
  action digest. An `approved` decision is checked by mctl-api against its
  `aar_` on exactly these fields, so they come from the same function that
  created the approval;
- `approval_id` (the `aar_`) on `approved` and on the `approval_*` refusals
  that name one; actor (`type:id` of the sealed execution context), tenant
  (its scope) and trace id; the run's `wi_` work item when the evidence
  producer knows one. No `prn_`: the execution context does not carry one;
- on attempts, the runtime execution (`ex-`), an `ev-` evidence id or `inv_`
  commerce invocation when the call site has one, and an error *code*
  (`exception:calledprocesserror`), never a message.

The free-text decision `reason`, arguments, target text, tool input and tool
output never leave this process.

Delivery. Writes go through one background thread per process, in order, so
a decision is always written before its attempts and a terminal event after
its `started`. The event's `occurred_at` is stamped where it happened; only
the HTTP write is deferred. The side effect never waits for it. Credentials
are the execution-evidence producer's (`MCTL_EVIDENCE_WRITER_TOKEN`, the
`service:mctl-agents-evidence` principal, over https only); mctl-api admits
it to exactly these two routes, in their own 150/min bucket. Each write
carries a stable idempotency key, so a retry after a lost answer is a replay
(200), never a second record.

- 201 created, 200 replayed.
- 429, 5xx and transport errors are retried, at most `ATTEMPTS` times. A 503
  that persists is `unobservable`: mctl-api could not read a reference or
  has no store. It is never read as "not found", and nothing about the
  decision is assumed from it.
- Any other 4xx is a refusal (a producer or credential bug), not retried.
- At process exit the queue is drained for at most `EXIT_FLUSH_SECONDS`;
  what is left is counted as `dropped_unflushed`.

Never fatal, never silent. This is the evidence producer's contract: a
record is bookkeeping about an action, not part of it, so no failure here
changes a decision, an approval receipt, a side effect or an exit code. And
a write that did not land is never treated as recorded: it is counted under
its own reason (`stats()`) and printed as one `DECISION_RECORD` line. An
attempt event whose decision was not recorded, or a terminal event whose
`started` was not, is dropped and counted (`dropped_decision_unrecorded`,
`dropped_attempt_unopened`), never sent against a guessed id. A side effect
whose end this process did not observe (a process killed mid-call, an
interrupted tool call) gets no terminal event at all: mctl-api then reads
the attempt as `in_progress`, or `unknown` once a later attempt starts,
never as `failed`.
"""
from __future__ import annotations

import atexit
import collections
import json
import logging
import os
import queue
import re
import threading
import time
import uuid
import weakref
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

import httpx

from orchestrator.context_snapshot import hash_bytes

logger = logging.getLogger(__name__)

ENABLE_ENV = "MCTL_DECISION_RECORDS"
DECISIONS_PATH = "/api/v1/decisions"
LOG_PREFIX = "DECISION_RECORD"

REQUEST_TIMEOUT_SECONDS = 5.0
ATTEMPTS = 3
RETRY_DELAYS_SECONDS = (1.0, 2.0)
#: Writes waiting for the background thread. Past it a write is dropped and
#: counted: an unbounded queue against a dead store would grow with the run.
MAX_QUEUE = 2048
EXIT_FLUSH_SECONDS = 15.0
#: Open MCP tool calls tracked between PreToolUse and PostToolUse.
MAX_OPEN_TOOL_USES = 1024

# Results, also the keys of `stats()`. One write is counted under exactly one.
CREATED = "created"
REPLAYED = "replayed"
REFUSED = "refused"
UNOBSERVABLE = "unobservable"
RATE_LIMITED = "rate_limited"
UNDELIVERED = "undelivered"
DISABLED = "disabled"
BUILD_FAILED = "build_failed"
SKIPPED_NO_EXECUTION = "skipped_no_execution"
SKIPPED_UNDESCRIBABLE = "skipped_undescribable"
SKIPPED_NOT_PERMITTED = "skipped_not_permitted"
DROPPED_UNTRACKED = "dropped_untracked"
DROPPED_DECISION_UNRECORDED = "dropped_decision_unrecorded"
DROPPED_ATTEMPT_UNOPENED = "dropped_attempt_unopened"
DROPPED_QUEUE_FULL = "dropped_queue_full"
DROPPED_UNFLUSHED = "dropped_unflushed"
DUPLICATE_TERMINAL = "duplicate_terminal"
LEFT_OPEN_INTERRUPTED = "left_open_interrupted"
#: Write results that mean the record exists in mctl-api.
RECORDED = frozenset({CREATED, REPLAYED})

PHASE_STARTED = "started"
PHASE_SUCCEEDED = "succeeded"
PHASE_FAILED = "failed"
PHASE_PARTIAL = "partial"

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_VERDICTS = {"ALLOW": "allow", "DENY": "deny", "REQUIRE_APPROVAL": "require_approval"}
_DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
_CODE_RE = re.compile(r"[a-z0-9._:-]{1,64}")
_CODE_INVALID = re.compile(r"[^a-z0-9._:-]+")
_EVIDENCE_ID_RE = re.compile(r"ev-[0-9a-f]{16}")
_RUNTIME_ID_RE = re.compile(r"ex-[0-9a-f]{16}")
_INVOCATION_ID_RE = re.compile(r"inv_[A-Za-z0-9._:-]{1,252}")
_DECISION_ID_RE = re.compile(r"dec_[0-9a-f]{32}")

Post = Callable[[str, dict[str, Any], dict[str, str]], httpx.Response]


def enabled(environ: Mapping[str, str] | None = None) -> bool:
    env = os.environ if environ is None else environ
    return env.get(ENABLE_ENV, "").strip().lower() in _TRUTHY


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def target_digest(target: str) -> str:
    """mctl-api's `decisions.TargetDigest`: sha256 of the target's bytes."""
    return hash_bytes(target.encode("utf-8"))


def error_code(value: str) -> str:
    """`value` as an attempt `error_code` (`[a-z0-9._:-]{1,64}`): a code,
    never a message."""
    code = _CODE_INVALID.sub("-", (value or "").strip().lower()).strip("-")[:64].rstrip("-")
    return code or "unspecified"


def exception_code(exc: BaseException) -> str:
    """The error code of a side effect that raised: its exception class,
    never its text (which can carry URLs, stderr or payload fragments)."""
    return error_code(f"exception:{type(exc).__name__}")


# ---------------------------------------------------------------------------
# What one decision and one attempt are, on this side.
# ---------------------------------------------------------------------------
@dataclass
class _DecisionHandle:
    key: str
    body: dict[str, Any]
    permitted: bool
    #: Set by the writer thread once mctl-api stored (or replayed) the
    #: decision; read only there.
    decision_id: str = ""
    failure: str = ""
    starts: int = 0
    lock: threading.Lock = field(default_factory=threading.Lock)


class Attempt:
    """One run of a permitted side effect. `started` was enqueued when this
    object was made; call exactly one of `succeeded`, `failed`, `partial`
    once it ended. A second terminal call is ignored and counted."""

    def __init__(self, recorder: DecisionRecorder | None, handle: _DecisionHandle | None, local_no: int) -> None:
        self._recorder = recorder
        self._handle = handle
        self._local_no = local_no
        self._closed = False
        self._lock = threading.Lock()
        #: mctl-api's attempt number, set by the writer thread.
        self.attempt_no = 0
        self.failure = ""

    @property
    def tracked(self) -> bool:
        return self._recorder is not None and self._handle is not None

    def succeeded(self, *, evidence_id: str = "", result_digest: str = "", commerce_invocation_id: str = "") -> None:
        self._end(PHASE_SUCCEEDED, "", evidence_id, result_digest, commerce_invocation_id)

    def failed(self, code: str, *, evidence_id: str = "", commerce_invocation_id: str = "") -> None:
        self._end(PHASE_FAILED, code, evidence_id, "", commerce_invocation_id)

    def partial(self, code: str, *, evidence_id: str = "", commerce_invocation_id: str = "") -> None:
        """Some, not all, of the effect was applied. Never reported as a
        success, and a later attempt may still recover it."""
        self._end(PHASE_PARTIAL, code, evidence_id, "", commerce_invocation_id)

    def _end(self, phase: str, code: str, evidence_id: str, result_digest: str, invocation_id: str) -> None:
        try:
            with self._lock:
                if self._closed:
                    if self._recorder is not None:
                        self._recorder._count(DUPLICATE_TERMINAL)
                    return
                self._closed = True
            if self._recorder is not None and self._handle is not None:
                self._recorder._enqueue_terminal(
                    self, self._handle, phase, code, evidence_id, result_digest, invocation_id)
        except Exception as exc:  # noqa: BLE001 — a record never fails the action it describes
            logger.warning("decision records: could not record an attempt end (%s)", type(exc).__name__)

    # A context manager around one run of the side effect: an exception is
    # a `failed` attempt (and is re-raised), a normal exit `succeeded`
    # unless the body already ended the attempt (`partial`, say).
    def __enter__(self) -> Attempt:
        return self

    def __exit__(self, exc_type: Any, exc: BaseException | None, tb: Any) -> Literal[False]:
        if exc is not None:
            self.failed(exception_code(exc))
        else:
            self.succeeded()
        return False


#: What `start()` answers when nothing is recorded (off, untracked).
def _untracked() -> Attempt:
    return Attempt(None, None, 0)


# ---------------------------------------------------------------------------
# The recorder.
# ---------------------------------------------------------------------------
class DecisionRecorder:
    def __init__(
        self,
        *,
        token: str,
        base_url: str,
        post: Post | None = None,
        sleep: Callable[[float], None] = time.sleep,
        start_thread: bool = True,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._post = post or _default_post
        self._sleep = sleep
        self._queue: queue.Queue[Callable[[], None]] = queue.Queue(maxsize=MAX_QUEUE)
        self._counts: collections.Counter[str] = collections.Counter()
        self._counts_lock = threading.Lock()
        self._handles: dict[int, _DecisionHandle] = {}
        self._handles_lock = threading.Lock()
        self._tool_uses: collections.OrderedDict[str, Attempt] = collections.OrderedDict()
        self._logged_once: set[str] = set()
        self._thread: threading.Thread | None = None
        self._thread_lock = threading.Lock()
        self._start_thread = start_thread

    # -- counters --------------------------------------------------------
    def _count(self, result: str) -> None:
        with self._counts_lock:
            self._counts[result] += 1

    def stats(self) -> dict[str, int]:
        with self._counts_lock:
            return dict(self._counts)

    def _report(self, result: str, *, key: str = "", decision_id: str = "", phase: str = "",
                status: int = 0, detail: str = "", once: bool = False) -> None:
        """Count `result` and, for anything but a recorded write, print one
        `DECISION_RECORD` line. `once`: print only the first of its kind in
        this process (a reason that repeats for every decision of a run)."""
        self._count(result)
        if result in RECORDED:
            return
        if once:
            if result in self._logged_once:
                return
            self._logged_once.add(result)
        line: dict[str, Any] = {"result": result}
        for name, value in (("key", key), ("decision_id", decision_id), ("phase", phase), ("detail", detail)):
            if value:
                line[name] = value
        if status:
            line["http_status"] = status
        try:
            print(f"{LOG_PREFIX} {json.dumps(line, sort_keys=True)}", flush=True)
            logger.warning("decision record not written: %s", result)
        except Exception:  # noqa: BLE001, S110 — logging is best effort
            pass

    # -- the writer thread -----------------------------------------------
    def _ensure_thread(self) -> None:
        if not self._start_thread:
            return
        with self._thread_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._work, name="decision-records", daemon=True)
            self._thread.start()

    def _work(self) -> None:
        while True:
            job = self._queue.get()
            try:
                job()
            except Exception as exc:  # noqa: BLE001 — one bad write never stops the next
                self._report(BUILD_FAILED, detail=type(exc).__name__)
            finally:
                self._queue.task_done()

    def run_pending(self) -> None:
        """Run every queued write on this thread. Tests use it in place of
        the background thread (`start_thread=False`)."""
        while True:
            try:
                job = self._queue.get_nowait()
            except queue.Empty:
                return
            try:
                job()
            except Exception as exc:  # noqa: BLE001
                self._report(BUILD_FAILED, detail=type(exc).__name__)
            finally:
                self._queue.task_done()

    def _enqueue(self, job: Callable[[], None], *, key: str, phase: str) -> bool:
        try:
            self._queue.put_nowait(job)
        except queue.Full:
            self._report(DROPPED_QUEUE_FULL, key=key, phase=phase)
            return False
        self._ensure_thread()
        return True

    def flush(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for every queued write. True when
        the queue drained."""
        if not self._start_thread:
            self.run_pending()
            return True
        deadline = time.monotonic() + timeout
        with self._queue.all_tasks_done:
            while self._queue.unfinished_tasks:
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._queue.all_tasks_done.wait(left)
        return True

    def drain_at_exit(self) -> None:
        try:
            if not self.flush(EXIT_FLUSH_SECONDS):
                left = self._queue.unfinished_tasks
                for _ in range(left):
                    self._count(DROPPED_UNFLUSHED)
                self._report_line_only(DROPPED_UNFLUSHED, detail=str(left))
        except Exception:  # noqa: BLE001, S110 — exit must never fail on bookkeeping
            pass

    def _report_line_only(self, result: str, *, detail: str) -> None:
        try:
            print(f"{LOG_PREFIX} {json.dumps({'result': result, 'detail': detail}, sort_keys=True)}", flush=True)
        except Exception:  # noqa: BLE001, S110
            pass

    # -- HTTP --------------------------------------------------------------
    def _write(self, path: str, body: dict[str, Any]) -> tuple[str, int, str, Any]:
        """POST one record; `(result, http_status, error_code, json)`.
        Never raises. Every attempt sends the same body (and idempotency
        key), so a retry is a replay."""
        url = self._base_url + path
        headers = {"Authorization": f"Bearer {self._token}"}
        status, detail = 0, ""
        resp: httpx.Response | None = None
        for n in range(1, ATTEMPTS + 1):
            try:
                resp = self._post(url, body, headers)
            except httpx.HTTPError as exc:
                status, detail, resp = 0, type(exc).__name__, None
            except Exception as exc:  # noqa: BLE001 — a broken transport is undelivered, not a crash
                return UNDELIVERED, 0, type(exc).__name__, None
            else:
                status = resp.status_code
                if status in (200, 201):
                    try:
                        data = resp.json()
                    except Exception:  # noqa: BLE001 — stored, but the answer is unreadable
                        data = None
                    return (CREATED if status == 201 else REPLAYED), status, "", data
                detail = _error_code_of(resp)
                if status < 500 and status != 429:
                    return REFUSED, status, detail, None
            if n < ATTEMPTS:
                delay = RETRY_DELAYS_SECONDS[min(n - 1, len(RETRY_DELAYS_SECONDS) - 1)]
                self._sleep(_retry_after(resp if status else None, default=delay))
        if status == 503:
            return UNOBSERVABLE, status, detail, None
        if status == 429:
            return RATE_LIMITED, status, detail, None
        return UNDELIVERED, status, detail, None

    # -- decisions ---------------------------------------------------------
    def record_decision(self, request: Any, decision: Any) -> None:
        key = f"mctl-agents.{uuid.uuid4().hex}"
        try:
            body, skip = decision_body(request, decision, key=key, decided_at=_now())
        except Exception as exc:  # noqa: BLE001 — an undescribable decision is counted, not raised
            self._report(BUILD_FAILED, key=key, detail=type(exc).__name__)
            return
        if body is None:
            self._report(skip, key=key, once=True)
            return
        handle = _DecisionHandle(key=key, body=body, permitted=bool(getattr(decision, "permitted", False)))
        self._track(decision, handle)
        self._enqueue(lambda: self._write_decision(handle), key=key, phase="decision")

    def _track(self, decision: Any, handle: _DecisionHandle) -> None:
        """Remember `decision` -> its handle for as long as the Decision
        object lives, so `start(decision)` finds the record it belongs to.
        By identity, not equality: two equal decisions are two records."""
        ident = id(decision)
        try:
            weakref.finalize(decision, self._forget, ident, handle)
        except TypeError:
            return
        with self._handles_lock:
            self._handles[ident] = handle

    def _forget(self, ident: int, handle: _DecisionHandle) -> None:
        with self._handles_lock:
            if self._handles.get(ident) is handle:
                del self._handles[ident]

    def _write_decision(self, handle: _DecisionHandle) -> None:
        result, status, detail, data = self._write(DECISIONS_PATH, handle.body)
        if result in RECORDED:
            decision_id = _get(data, "decision", "id")
            if isinstance(decision_id, str) and _DECISION_ID_RE.fullmatch(decision_id):
                handle.decision_id = decision_id
                self._report(result)
                return
            # Stored, but which id it got cannot be read: its attempts
            # cannot be addressed, so they are dropped, not guessed.
            result, detail = UNOBSERVABLE, "unreadable-decision-id"
        handle.failure = result
        self._report(result, key=handle.key, phase="decision", status=status, detail=detail)

    # -- attempts ----------------------------------------------------------
    def start(self, decision: Any, *, runtime_execution_id: str = "") -> Attempt:
        with self._handles_lock:
            handle = self._handles.get(id(decision))
        if handle is None:
            self._report(DROPPED_UNTRACKED, phase=PHASE_STARTED, once=True)
            return _untracked()
        if not handle.permitted:
            self._report(SKIPPED_NOT_PERMITTED, key=handle.key, phase=PHASE_STARTED)
            return _untracked()
        with handle.lock:
            handle.starts += 1
            local_no = handle.starts
        attempt = Attempt(self, handle, local_no)
        body: dict[str, Any] = {
            "phase": PHASE_STARTED,
            "occurred_at": _now(),
            "idempotency_key": f"{handle.key}:s{local_no}",
        }
        runtime_id = runtime_execution_id or _runtime_id_of(handle)
        if runtime_id:
            body["runtime_execution_id"] = runtime_id
        self._enqueue(lambda: self._write_started(handle, attempt, body), key=handle.key, phase=PHASE_STARTED)
        return attempt

    def _write_started(self, handle: _DecisionHandle, attempt: Attempt, body: dict[str, Any]) -> None:
        if not handle.decision_id:
            attempt.failure = DROPPED_DECISION_UNRECORDED
            self._report(DROPPED_DECISION_UNRECORDED, key=handle.key, phase=PHASE_STARTED, detail=handle.failure)
            return
        result, status, detail, data = self._write(f"{DECISIONS_PATH}/{handle.decision_id}/attempts", body)
        if result in RECORDED:
            attempt_no = _get(data, "event", "attempt_no")
            if isinstance(attempt_no, int) and not isinstance(attempt_no, bool) and attempt_no >= 1:
                attempt.attempt_no = attempt_no
                self._report(result)
                return
            result, detail = UNOBSERVABLE, "unreadable-attempt-no"
        attempt.failure = result
        self._report(result, key=handle.key, decision_id=handle.decision_id, phase=PHASE_STARTED,
                     status=status, detail=detail)

    def _enqueue_terminal(self, attempt: Attempt, handle: _DecisionHandle, phase: str, code: str,
                          evidence_id: str, result_digest: str, invocation_id: str) -> None:
        body: dict[str, Any] = {
            "phase": phase,
            "occurred_at": _now(),
            "idempotency_key": f"{handle.key}:e{attempt._local_no}",
        }
        if phase != PHASE_SUCCEEDED:
            body["error_code"] = error_code(code)
        if evidence_id and _EVIDENCE_ID_RE.fullmatch(evidence_id):
            body["evidence_id"] = evidence_id
        if result_digest and _DIGEST_RE.fullmatch(result_digest):
            body["result_digest"] = result_digest
        if invocation_id and _INVOCATION_ID_RE.fullmatch(invocation_id):
            body["commerce_invocation_id"] = invocation_id
        self._enqueue(lambda: self._write_terminal(handle, attempt, body), key=handle.key, phase=phase)

    def _write_terminal(self, handle: _DecisionHandle, attempt: Attempt, body: dict[str, Any]) -> None:
        phase = body["phase"]
        if not handle.decision_id:
            self._report(DROPPED_DECISION_UNRECORDED, key=handle.key, phase=phase, detail=handle.failure)
            return
        if not attempt.attempt_no:
            self._report(DROPPED_ATTEMPT_UNOPENED, key=handle.key, decision_id=handle.decision_id, phase=phase,
                         detail=attempt.failure)
            return
        result, status, detail, _ = self._write(
            f"{DECISIONS_PATH}/{handle.decision_id}/attempts", {**body, "attempt_no": attempt.attempt_no})
        self._report(result, key=handle.key, decision_id=handle.decision_id, phase=phase, status=status,
                     detail=detail)

    # -- MCP tool calls: PreToolUse opens, PostToolUse(Failure) closes ------
    def start_tool_use(self, tool_use_id: str, decision: Any) -> None:
        attempt = self.start(decision)
        if not attempt.tracked or not tool_use_id:
            return
        with self._handles_lock:
            self._tool_uses[tool_use_id] = attempt
            while len(self._tool_uses) > MAX_OPEN_TOOL_USES:
                # Never closed: mctl-api reads it in_progress/unknown, which
                # is what this process knows about it.
                self._tool_uses.popitem(last=False)

    def finish_tool_use(self, tool_use_id: str, phase: str, code: str = "") -> None:
        with self._handles_lock:
            attempt = self._tool_uses.pop(tool_use_id, None) if tool_use_id else None
        if attempt is None:
            return
        if phase == PHASE_SUCCEEDED:
            attempt.succeeded()
        else:
            attempt.failed(code)

    def abandon_tool_use(self, tool_use_id: str) -> None:
        """The call ended without an observed outcome (interrupted): stop
        tracking it and send no terminal event."""
        with self._handles_lock:
            attempt = self._tool_uses.pop(tool_use_id, None) if tool_use_id else None
        if attempt is not None:
            self._report(LEFT_OPEN_INTERRUPTED, key=attempt._handle.key if attempt._handle else "")


def decision_body(request: Any, decision: Any, *, key: str, decided_at: str) -> tuple[dict[str, Any] | None, str]:
    """`(body, "")` for `POST /api/v1/decisions`, or `(None, reason)` when
    the decision lacks a field mctl-api requires. Ids and digests only."""
    from orchestrator import action_approvals

    execution_id = str(getattr(request, "execution_id", "") or "")
    actor = str(getattr(request, "actor", "") or "")
    if not execution_id or not actor:
        # No sealed execution context (a poller, local run): the record
        # would name no execution anyone can join on.
        return None, SKIPPED_NO_EXECUTION
    action_digest = str(getattr(decision, "action_digest", "") or "")
    args_digest = str(getattr(request, "args_digest", "") or "")
    if not (_DIGEST_RE.fullmatch(action_digest) and _DIGEST_RE.fullmatch(args_digest)):
        return None, SKIPPED_UNDESCRIBABLE
    rule_id = str(getattr(decision, "rule_id", "") or "")
    policy_version = str(getattr(decision, "policy_version", "") or "")
    intent = action_approvals.intent_for(request, rule_id=rule_id, policy_version=policy_version)
    code = str(getattr(decision, "code", "") or "")
    body: dict[str, Any] = {
        "execution_id": intent.execution_id,
        "policy_version": intent.policy_version,
        "verdict": _VERDICTS.get(str(getattr(decision, "verdict", "")), ""),
        "code": code,
        "actor": actor,
        "action_kind": intent.action_kind,
        "operation": str(getattr(request, "operation", "") or ""),
        "target_digest": target_digest(intent.target),
        "args_digest": intent.args_digest,
        "action_digest": action_digest,
        "decided_at": decided_at,
        "idempotency_key": key,
    }
    if intent.policy_rule_id:
        body["rule_id"] = intent.policy_rule_id
    approval_ref = str(getattr(decision, "approval_ref", "") or "")
    if approval_ref.startswith("aar_") and (code == "approved" or code.startswith("approval_")):
        body["approval_id"] = approval_ref
    trace_id = str(getattr(request, "trace_id", "") or "")
    if trace_id:
        body["trace_id"] = trace_id
    tenant = _tenant_of(execution_id)
    if tenant:
        body["tenant"] = tenant
    work_item_id = _work_item_of_current_run()
    if work_item_id:
        body["work_item_id"] = work_item_id
    return body, ""


def _runtime_id_of(handle: _DecisionHandle) -> str:
    execution_id = str(handle.body.get("execution_id", ""))
    return execution_id if _RUNTIME_ID_RE.fullmatch(execution_id) else ""


_TENANTS: dict[tuple[str, str], str] = {}


def _tenant_of(execution_id: str) -> str:
    """The sealed execution context's scope tenant, when that context is
    the one `execution_id` names. Read once per (file, execution)."""
    from orchestrator.execution_identity import MCTL_EXECUTION_CONTEXT_FILE_ENV, load_from_environment

    path = os.environ.get(MCTL_EXECUTION_CONTEXT_FILE_ENV, "").strip()
    if not path:
        return ""
    cache_key = (path, execution_id)
    if cache_key not in _TENANTS:
        try:
            ctx = load_from_environment(executor_type="system")
            tenant = ctx.scope.tenant if ctx.context_id == execution_id else ""
        except Exception:  # noqa: BLE001 — the tenant is optional; an unreadable context leaves it out
            tenant = ""
        _TENANTS[cache_key] = tenant if isinstance(tenant, str) else ""
    return _TENANTS[cache_key]


def _work_item_of_current_run() -> str:
    try:
        from orchestrator import evidence_producer

        run = evidence_producer.current()
    except Exception:  # noqa: BLE001 — enrichment only
        return ""
    work_item_id = getattr(run, "work_item_id", "") if run is not None else ""
    return work_item_id if isinstance(work_item_id, str) and work_item_id.startswith("wi_") else ""


def _get(data: Any, outer: str, inner: str) -> Any:
    block = data.get(outer) if isinstance(data, Mapping) else None
    return block.get(inner) if isinstance(block, Mapping) else None


def _default_post(url: str, body: dict[str, Any], headers: dict[str, str]) -> httpx.Response:
    return httpx.post(url, json=body, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS)


def _error_code_of(resp: httpx.Response) -> str:
    from orchestrator import evidence_producer

    return evidence_producer._error_code(resp)


def _retry_after(resp: httpx.Response | None, *, default: float) -> float:
    from orchestrator import evidence_producer

    return evidence_producer._retry_after(resp, default=default)


# ---------------------------------------------------------------------------
# The process's recorder, and the module API the call sites use. Every
# function below is a no-op when the producer is off and never raises.
# ---------------------------------------------------------------------------
_RECORDER: DecisionRecorder | None = None
_RECORDER_LOCK = threading.Lock()
_OFF_LOGGED = False


def recorder() -> DecisionRecorder | None:
    """The process's recorder, built on first use; None when off."""
    global _RECORDER, _OFF_LOGGED
    if _RECORDER is not None:
        return _RECORDER
    if not enabled():
        return None
    with _RECORDER_LOCK:
        if _RECORDER is not None:
            return _RECORDER
        from orchestrator import evidence_producer

        token, base_url, why = evidence_producer.writer_credentials(os.environ)
        if not token:
            if not _OFF_LOGGED:
                _OFF_LOGGED = True
                logger.warning("decision records are on but cannot be written (%s): none will be", why)
                print(f"{LOG_PREFIX} {json.dumps({'result': DISABLED, 'detail': why}, sort_keys=True)}", flush=True)
            return None
        _RECORDER = DecisionRecorder(token=token, base_url=base_url)
        atexit.register(_RECORDER.drain_at_exit)
        return _RECORDER


def _safely(action: Callable[[DecisionRecorder], Any], default: Any = None) -> Any:
    try:
        rec = recorder()
        return action(rec) if rec is not None else default
    except Exception as exc:  # noqa: BLE001 — a record never fails the action it describes
        logger.warning("decision records: %s: %s", type(exc).__name__, exc)
        return default


def record_decision(request: Any, decision: Any) -> None:
    """Called by `policy_checkpoint.emit` for every decision."""
    _safely(lambda rec: rec.record_decision(request, decision))


def start(decision: Any, *, runtime_execution_id: str = "") -> Attempt:
    """Open an attempt of `decision`'s side effect: call immediately before
    it runs, once per run (a retry calls this again)."""
    attempt = _safely(lambda rec: rec.start(decision, runtime_execution_id=runtime_execution_id))
    return attempt if isinstance(attempt, Attempt) else _untracked()


def start_tool_use(tool_use_id: str | None, decision: Any) -> None:
    """The MCP checkpoint hook permitted `tool_use_id`: it runs next."""
    _safely(lambda rec: rec.start_tool_use(tool_use_id or "", decision))


def finish_tool_use(tool_use_id: str | None, phase: str, code: str = "") -> None:
    _safely(lambda rec: rec.finish_tool_use(tool_use_id or "", phase, code))


def abandon_tool_use(tool_use_id: str | None) -> None:
    _safely(lambda rec: rec.abandon_tool_use(tool_use_id or ""))


def flush(timeout: float = EXIT_FLUSH_SECONDS) -> bool:
    result = _safely(lambda rec: rec.flush(timeout), default=True)
    return bool(result)


def stats() -> dict[str, int]:
    rec = _RECORDER
    return rec.stats() if rec is not None else {}


def _install_for_tests(rec: DecisionRecorder | None) -> None:
    global _RECORDER, _OFF_LOGGED
    _RECORDER = rec
    _OFF_LOGGED = False
    _TENANTS.clear()
