"""HTTP client for the mctl-api `WorkItem` surface (mctl-api#227).

Synchronous `urllib`, structurally copied from
`orchestrator/lifecycle/client.py`'s `OwnershipClient`: same `MCTL_TOKEN`
bearer auth, same https-only refusal, same no-redirect opener, same
"uncertainty is a value, never an exception" contract. There is exactly one
caller shape today (an ordinary CLI process — `run_issue_investigator.py`
inside an Argo pod), so unlike `lifecycle/client.py` this module defines no
async counterpart; one can be added the same way `activities/lifecycle.py`
was, if a Temporal activity ever needs one.

Route strings live in one module-level table (`ROUTES`) so a rename is a
one-line change and every test monkeypatches the transport rather than a URL
(see the "Exact mctl-api route shape" open question in requirements.md).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any
from urllib.parse import quote

from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context.contract import (
    WORK_ITEM_FOUND,
    WORK_ITEM_UNKNOWN,
    WorkItemAnswer,
    answer_from,
    executions_from,
    latest_execution_id_of,
    with_executions,
)
from orchestrator.work_context.executions import (
    ATTACH_EXECUTION_OPERATION,
    EXECUTION_REFUSED,
    EXECUTION_UNKNOWN,
    EngineRun,
    ExecutionAnswer,
    answer_from_attach,
    attach_body,
)
from orchestrator.work_context.snapshots import (
    SEAL_SNAPSHOT_OPERATION,
    SNAPSHOT_REFUSED,
    SNAPSHOT_UNKNOWN,
    SnapshotAnswer,
    answer_from_read,
    answer_from_seal,
)

DEFAULT_TIMEOUT_S = 10

# mctl-api's `workitem/v1` read routes (internal/api/router.go there).
ROUTES = {
    "get_work_item": "/api/v1/work-items/{id}",
    # GET lists the ledger; POST attaches an execution or advances its
    # phase, idempotent on (engine, engine_ref) (mctlhq/mctl-agents#455).
    "list_executions": "/api/v1/work-items/{id}/executions",
    # Sealed ContextSnapshots (mctl-api#362, mctlhq/mctl-agents#431).
    "execution_snapshot": "/api/v1/work-items/{id}/executions/{execution_id}/snapshot",
    # Execution requests (mctl-api#368, mctlhq/mctl-agents#461). Claim,
    # fulfil and reject are the service principal's alone.
    "execution_request": "/api/v1/work-items/{id}/execution-requests/{request_id}",
    "claim_execution_request": "/api/v1/execution-requests/claim",
    "fulfil_execution_request": "/api/v1/execution-requests/{request_id}/fulfil",
    "reject_execution_request": "/api/v1/execution-requests/{request_id}/reject",
}


def api_base() -> str:
    return os.environ.get("MCTL_API_BASE_URL", "https://api.mctl.ai").rstrip("/")


class WorkItemUnavailable(RuntimeError):
    """The store could not be reached or is not configured.

    Raised only by the low-level transport. Every public method converts it
    into a `WORK_ITEM_UNKNOWN` answer, mirroring
    `orchestrator/lifecycle/client.py`'s `OwnershipUnavailable`.
    """


def _no_redirect_opener() -> urllib.request.OpenerDirector:
    """Opener that surfaces a 3xx as an HTTPError instead of following it —
    mirrors `orchestrator/lifecycle/client.py`'s `_no_redirect_opener`.
    Following a redirect would let a misconfigured MCTL_API_BASE_URL
    silently answer work-item questions from somewhere else."""

    class _NoRedirects(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            return None

    return urllib.request.build_opener(_NoRedirects)


def _q(value: str) -> str:
    return quote(str(value), safe="")


class _HTTPResult:
    __slots__ = ("body_empty", "payload", "status", "status_path")

    def __init__(self, status: int, payload: Any, status_path: str = "", body_empty: bool = False) -> None:
        self.status = status
        self.body_empty = body_empty
        self.payload = payload if isinstance(payload, dict) else {}
        self.status_path = status_path


class WorkItemClient:
    """Synchronous client. Safe in Argo pods and CLI entry points."""

    def __init__(self, base_url: str | None = None, token: str | None = None, timeout: int = DEFAULT_TIMEOUT_S) -> None:
        self._base = (base_url or api_base()).rstrip("/")
        self._token = token if token is not None else os.environ.get("MCTL_TOKEN", "").strip()
        self._timeout = timeout
        self._opener = _no_redirect_opener()

    # -- transport ------------------------------------------------------

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> _HTTPResult:
        if not self._token:
            raise WorkItemUnavailable("MCTL_TOKEN is not set")
        url = f"{self._base}{path}"
        if not url.startswith("https://"):
            # Refuse non-https rather than sending a bearer token in the
            # clear (also satisfies ruff S310), mirroring
            # orchestrator/lifecycle/client.py.
            raise WorkItemUnavailable(f"refusing non-https MCTL_API_BASE_URL: {self._base}")
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=data, method=method)  # noqa: S310 — scheme checked above
        req.add_header("Authorization", f"Bearer {self._token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=self._timeout) as resp:
                body = resp.read()
                status = resp.getcode() or 200
        except urllib.error.HTTPError as exc:
            try:
                parsed_any = json.loads(exc.read() or b"{}")
            except Exception:  # noqa: BLE001 — a failed error-body read is still just an error
                parsed_any = {}
            return _HTTPResult(status=exc.code, payload=parsed_any, status_path=path)
        except Exception as exc:
            raise WorkItemUnavailable(str(exc)) from exc
        try:
            return _HTTPResult(
                status=status, payload=json.loads(body or b"{}"), status_path=path, body_empty=not body,
            )
        except ValueError as exc:
            raise WorkItemUnavailable(f"malformed response: {exc}") from exc

    # -- reads ------------------------------------------------------------

    def get(self, work_item_id: str) -> WorkItemAnswer:
        """The work item and its full execution ledger.

        mctl-api's view carries only `latest_execution`, so a FOUND item is
        completed from the executions route. That second read is all or
        nothing: if it fails, or the ledger does not end on exactly the
        view's own latest execution, the answer is WORK_ITEM_UNKNOWN — never
        a FOUND item with an understated or overstated execution list. An
        execution attached between the two reads is the overstated case, so
        UNKNOWN here can mean "read again", not only "the store is down"."""
        try:
            res = self._request("GET", ROUTES["get_work_item"].format(id=_q(work_item_id)))
        except WorkItemUnavailable as exc:
            return WorkItemAnswer(verdict=WORK_ITEM_UNKNOWN, reason=str(exc))
        answer = answer_from(res.status, res.payload, path=res.status_path, body_empty=res.body_empty)
        if answer.verdict != WORK_ITEM_FOUND or answer.item is None:
            return answer
        item = answer.item
        if item.work_item_id != work_item_id:
            return WorkItemAnswer(
                verdict=WORK_ITEM_UNKNOWN,
                reason=f"asked for {work_item_id!r}, the store answered {item.work_item_id!r}",
                accepted=True,
            )
        try:
            ex = self._request("GET", ROUTES["list_executions"].format(id=_q(work_item_id)))
        except WorkItemUnavailable as exc:
            return WorkItemAnswer(verdict=WORK_ITEM_UNKNOWN, reason=f"executions: {exc}")
        executions, why = executions_from(ex.status, ex.payload, work_item_id)
        if executions is None:
            # `accepted` only when the store actually answered the read.
            return WorkItemAnswer(
                verdict=WORK_ITEM_UNKNOWN, reason=why, accepted=200 <= ex.status < 300 and not ex.body_empty
            )
        # The two reads must describe the same moment: the ledger ends on
        # exactly the view's latest execution (or both are empty). An
        # execution attached between the reads would otherwise overstate
        # the ledger, and a stale ledger understate it.
        latest = latest_execution_id_of(res.payload)
        ledger_latest = executions[-1].execution_id if executions else ""
        if ledger_latest != latest:
            return WorkItemAnswer(
                verdict=WORK_ITEM_UNKNOWN,
                reason=(
                    f"executions: the ledger ends on {ledger_latest!r}, the view's latest "
                    f"execution is {latest!r}; read again"
                ),
                accepted=True,
            )
        # v1 executions carry no surface/actor kinds, so the verdict the view
        # already earned is unchanged.
        return WorkItemAnswer(verdict=WORK_ITEM_FOUND, item=with_executions(item, executions), accepted=True)

    # -- sealed snapshots (mctlhq/mctl-agents#431) ------------------------

    def execution_snapshot(self, work_item_id: str, execution_id: str) -> SnapshotAnswer:
        """The snapshot `execution_id` sealed: REPLAYED with its id, ABSENT
        when it sealed none, UNKNOWN otherwise."""
        path = ROUTES["execution_snapshot"].format(id=_q(work_item_id), execution_id=_q(execution_id))
        try:
            res = self._request("GET", path)
        except WorkItemUnavailable as exc:
            return SnapshotAnswer(SNAPSHOT_UNKNOWN, reason=str(exc))
        return answer_from_read(res.status, res.payload, execution_id=execution_id)

    def seal_snapshot(self, work_item_id: str, execution_id: str, body: dict[str, Any]) -> SnapshotAnswer:
        """Seal `body` as `execution_id`'s snapshot. The policy checkpoint
        (#197) sits immediately before the POST; a refusal is answered as
        REFUSED and nothing is sent."""
        from orchestrator import policy_checkpoint

        decision = policy_checkpoint.checkpoint(
            policy_checkpoint.MCTL_WORK_ITEM_WRITE,
            SEAL_SNAPSHOT_OPERATION,
            work_item_id,
            body,
            metadata={"work_item_id": work_item_id, "execution_id": execution_id},
        )
        if not decision.permitted:
            return SnapshotAnswer(
                SNAPSHOT_REFUSED, content_hash=str(body.get("content_hash", "")),
                reason=f"policy {decision.verdict} ({decision.code})",
            )
        path = ROUTES["execution_snapshot"].format(id=_q(work_item_id), execution_id=_q(execution_id))
        try:
            res = self._request("POST", path, body)
        except WorkItemUnavailable as exc:
            return SnapshotAnswer(SNAPSHOT_UNKNOWN, content_hash=str(body.get("content_hash", "")), reason=str(exc))
        return answer_from_seal(
            res.status, res.payload, content_hash=str(body.get("content_hash", "")), execution_id=execution_id
        )

    # -- this run's own execution (mctlhq/mctl-agents#455) ---------------

    def attach_execution(self, work_item_id: str, run: EngineRun, phase: str) -> ExecutionAnswer:
        """Attach `run` to the work item in `phase`, or advance the execution
        already attached under the same `(engine, engine_ref)` to it. The
        store answers the same `we_...` id for the same engine run, so a
        retry is the same execution by construction.

        The policy checkpoint (#197) sits immediately before the POST; a
        refusal is answered as REFUSED and nothing is sent."""
        from orchestrator import policy_checkpoint

        body = attach_body(run, phase)
        decision = policy_checkpoint.checkpoint(
            policy_checkpoint.MCTL_WORK_ITEM_WRITE,
            ATTACH_EXECUTION_OPERATION,
            work_item_id,
            body,
            metadata={
                "work_item_id": work_item_id, "engine": run.engine, "engine_ref": run.engine_ref, "phase": phase,
            },
        )
        if not decision.permitted:
            return ExecutionAnswer(EXECUTION_REFUSED, reason=f"policy {decision.verdict} ({decision.code})")
        path = ROUTES["list_executions"].format(id=_q(work_item_id))
        try:
            res = self._request("POST", path, body)
        except WorkItemUnavailable as exc:
            return ExecutionAnswer(EXECUTION_UNKNOWN, reason=str(exc))
        return answer_from_attach(res.status, res.payload, work_item_id=work_item_id, run=run, phase=phase)

    # -- execution requests (mctl-api#368, mctlhq/mctl-agents#461) --------
    #
    # The claim token fences a claim: fulfil and reject must present it. It
    # is a bearer credential for that one claim, so it is sent in the body
    # and nowhere else — not in the policy checkpoint's args (whose digest
    # is logged), not in its metadata, not in any log line.

    def _governed(self, operation: str, target: str, args: dict[str, Any], metadata: dict[str, str]) -> str:
        """The policy checkpoint (#197) immediately before a mutation: ""
        when permitted, else why not."""
        from orchestrator import policy_checkpoint

        decision = policy_checkpoint.checkpoint(
            policy_checkpoint.MCTL_WORK_ITEM_WRITE, operation, target, args, metadata=metadata,
        )
        return "" if decision.permitted else f"policy {decision.verdict} ({decision.code})"

    def execution_request(self, work_item_id: str, request_id: str) -> xr.RequestAnswer:
        """One execution request, as whoever can see the item reads it."""
        path = ROUTES["execution_request"].format(id=_q(work_item_id), request_id=_q(request_id))
        try:
            res = self._request("GET", path)
        except WorkItemUnavailable as exc:
            return xr.RequestAnswer(xr.UNKNOWN, reason=str(exc))
        return xr.answer_from_read(res.status, res.payload, request_id=request_id)

    def claim_execution_request(self, lease_seconds: int) -> xr.RequestAnswer:
        """Claim the oldest claimable request under a lease of
        `lease_seconds`. NONE_CLAIMABLE on a 204."""
        body = {"lease_seconds": lease_seconds}
        refused = self._governed(xr.CLAIM_OPERATION, "execution-requests", body, {"lease_seconds": str(lease_seconds)})
        if refused:
            return xr.RequestAnswer(xr.REFUSED, reason=refused)
        try:
            res = self._request("POST", ROUTES["claim_execution_request"], body)
        except WorkItemUnavailable as exc:
            return xr.RequestAnswer(xr.UNKNOWN, reason=str(exc))
        return xr.answer_from_claim(res.status, res.payload, body_empty=res.body_empty)

    def fulfil_execution_request(
        self, request: xr.ExecutionRequest, claim_token: str, engine: str, engine_ref: str
    ) -> xr.RequestAnswer:
        """Attach the canonical execution for `request`: the engine run
        `(engine, engine_ref)`. The same holder repeating the same engine run
        gets the same execution back."""
        governed = {"execution_request_id": request.request_id, "engine": engine, "engine_ref": engine_ref}
        refused = self._governed(
            xr.FULFIL_OPERATION, request.work_item_id, governed, {"work_item_id": request.work_item_id, **governed},
        )
        if refused:
            return xr.RequestAnswer(xr.REFUSED, reason=refused)
        path = ROUTES["fulfil_execution_request"].format(request_id=_q(request.request_id))
        body = {"claim_token": claim_token, "engine": engine, "engine_ref": engine_ref}
        try:
            res = self._request("POST", path, body)
        except WorkItemUnavailable as exc:
            return xr.RequestAnswer(xr.UNKNOWN, reason=str(exc))
        return xr.answer_from_fulfil(
            res.status, res.payload, request_id=request.request_id, engine=engine, engine_ref=engine_ref
        )

    def reject_execution_request(
        self, request: xr.ExecutionRequest, claim_token: str, reason: str
    ) -> xr.RequestAnswer:
        """Close the claimed `request` without an execution, for `reason`
        (one of the typed reasons in `execution_requests`)."""
        governed = {"execution_request_id": request.request_id, "reason": reason}
        refused = self._governed(
            xr.REJECT_OPERATION, request.work_item_id, governed, {"work_item_id": request.work_item_id, **governed},
        )
        if refused:
            return xr.RequestAnswer(xr.REFUSED, reason=refused)
        path = ROUTES["reject_execution_request"].format(request_id=_q(request.request_id))
        try:
            res = self._request("POST", path, {"claim_token": claim_token, "reason": reason})
        except WorkItemUnavailable as exc:
            return xr.RequestAnswer(xr.UNKNOWN, reason=str(exc))
        return xr.answer_from_reject(res.status, res.payload, request_id=request.request_id)
