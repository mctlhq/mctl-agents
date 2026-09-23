"""This run's own execution identity in the work-item store
(mctlhq/mctl-agents#455 item 1, owner decision B on #431).

mctl-api is the identity authority. An execution is `(work_item_id, engine,
engine_ref)`: `POST /api/v1/work-items/{id}/executions` is idempotent on that
triple, so the same engine run always gets the same `we_...` id back (201
the first time, 200 after), and a different engine run gets a new one. This
module is the client side of that rule, stdlib-only like the rest of
`orchestrator.work_context`:

- `engine_ref_from_env` names the engine run this process belongs to: the
  Argo `{{workflow.name}}` (`WORKFLOW_NAME`) unless `MCTL_ENGINE_REF` says
  otherwise. A retried step of the same workflow has the same name and so
  the same execution; a new investigation is a new workflow and a new one.
  No engine ref means no identity. Nothing here ever invents one locally.
- `answer_from_attach` classifies mctl-api's answer. A 2xx counts only when
  it describes exactly the run that was sent.
- `resolve_identity` turns a flag or an attach into the execution id,
  its store attempt (`execution_sequence`), and the ledger that contains it.

The rollout gate and what a refusal costs are the caller's decisions
(`run_issue_investigator.investigate`); the policy checkpoint (#197) sits in
`WorkItemClient.attach_execution`, immediately before the POST.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from orchestrator.context_snapshot import MAX_WORK_CONTEXT_ID_LENGTH
from orchestrator.work_context.contract import (
    EXECUTION_ENGINES,
    ExecutionRef,
    WorkItem,
    with_executions,
)
from orchestrator.work_context.snapshots import is_store_execution

#: The policy checkpoint's operation for attaching this run's own execution
#: or advancing its phase (#197).
ATTACH_EXECUTION_OPERATION = "attach:work-item-execution"

ENGINE_ARGO = "argo"

# mctl-api's `workitems.Phase*` constants (internal/workitems/types.go).
PHASE_RUNNING = "Running"
PHASE_SUCCEEDED = "Succeeded"
PHASE_FAILED = "Failed"

#: mctl-api's `workitems.MaxEngineRefBytes`.
MAX_ENGINE_REF_BYTES = 256

ENGINE_ENV_VAR = "MCTL_ENGINE"
ENGINE_REF_ENV_VAR = "MCTL_ENGINE_REF"
#: The Argo `{{workflow.name}}`, as the investigate CWFT's templates name it.
WORKFLOW_NAME_ENV_VAR = "WORKFLOW_NAME"
#: "false" when the engine will run another attempt of this same engine run
#: after a failure (the investigate CWFT's fallback step). Such a failure
#: must leave the execution open: the store refuses to reopen an ended one.
FINAL_ATTEMPT_ENV_VAR = "MCTL_ENGINE_FINAL_ATTEMPT"

# mctl-api's typed 409 code (internal/api/handlers_work_items.go) for
# another non-terminal execution. Every other 409, `invalid_transition`
# included, is a definite refusal through the generic 4xx arm of
# `answer_from_attach`.
EXECUTION_ACTIVE_CODE = "execution_active"

SCHEMA_VERSION = "workitem/v1"

EXECUTION_ATTACHED = "execution-attached"
EXECUTION_EXISTING = "execution-existing"
#: Another non-terminal execution holds the work item (409 execution_active).
EXECUTION_ACTIVE = "execution-active"
#: A definite no: the policy checkpoint, an ended execution, a terminal
#: work item, or any other 4xx.
EXECUTION_REFUSED = "execution-refused"
EXECUTION_UNKNOWN = "execution-unknown"


@dataclass(frozen=True)
class EngineRun:
    """The engine run this process belongs to."""

    engine: str
    engine_ref: str
    #: Which variable named it, for the log line.
    source: str = ""


def engine_ref_from_env() -> tuple[EngineRun | None, str]:
    """The engine run named by the environment, or None and why not."""
    # Normalised like final_attempt() and rollout.mode(): a casing typo in a
    # gitops env map must not cost the run its identity.
    engine = os.environ.get(ENGINE_ENV_VAR, "").strip().lower() or ENGINE_ARGO
    if engine not in EXECUTION_ENGINES:
        return None, f"{ENGINE_ENV_VAR}={engine!r} is not one of {sorted(EXECUTION_ENGINES)}"
    for var in (ENGINE_REF_ENV_VAR, WORKFLOW_NAME_ENV_VAR):
        ref = os.environ.get(var, "").strip()
        if not ref:
            continue
        if len(ref.encode("utf-8")) > MAX_ENGINE_REF_BYTES:
            return None, f"{var} exceeds {MAX_ENGINE_REF_BYTES} bytes"
        return EngineRun(engine=engine, engine_ref=ref, source=var), ""
    return None, f"neither {ENGINE_REF_ENV_VAR} nor {WORKFLOW_NAME_ENV_VAR} is set"


def final_attempt() -> bool:
    """Is this the engine's last attempt of this engine run? Default yes."""
    return os.environ.get(FINAL_ATTEMPT_ENV_VAR, "true").strip().lower() not in {"false", "no", "0", "off"}


@dataclass(frozen=True)
class ExecutionAnswer:
    verdict: str
    execution_id: str = ""
    attempt: int = 0
    phase: str = ""
    reason: str = ""

    @property
    def usable(self) -> bool:
        return self.verdict in (EXECUTION_ATTACHED, EXECUTION_EXISTING)


def attach_body(run: EngineRun, phase: str) -> dict[str, Any]:
    return {"engine": run.engine, "engine_ref": run.engine_ref, "phase": phase}


def answer_from_attach(
    status: int, payload: dict[str, Any], *, work_item_id: str, run: EngineRun, phase: str
) -> ExecutionAnswer:
    """Classify mctl-api's answer to an attach. A 2xx counts only when it
    describes exactly this work item, engine run and phase, under a store
    execution id."""
    if status in (200, 201):
        ex = payload.get("execution") if payload.get("schema_version") == SCHEMA_VERSION else None
        ex = ex if isinstance(ex, dict) else {}
        eid, attempt = ex.get("id"), ex.get("attempt")
        describes_ours = (
            ex.get("work_item_id") == work_item_id
            and ex.get("engine") == run.engine
            and ex.get("engine_ref") == run.engine_ref
            and ex.get("phase") == phase
        )
        if (
            not is_store_execution(eid)
            or not isinstance(eid, str)
            or len(eid) > MAX_WORK_CONTEXT_ID_LENGTH
            or not isinstance(attempt, int)
            or isinstance(attempt, bool)
            or attempt < 1
            or not describes_ours
        ):
            return ExecutionAnswer(
                EXECUTION_UNKNOWN,
                reason=f"HTTP {status} does not describe {run.engine}/{run.engine_ref} {phase} of {work_item_id}",
            )
        return ExecutionAnswer(
            EXECUTION_ATTACHED if status == 201 else EXECUTION_EXISTING,
            execution_id=eid, attempt=attempt, phase=phase,
        )
    code = payload.get("code")
    reason = f"HTTP {status} {code or ''}: {payload.get('error', '')}".strip()
    if status == 409 and code == EXECUTION_ACTIVE_CODE:
        return ExecutionAnswer(EXECUTION_ACTIVE, reason=reason)
    if 400 <= status < 500 and status not in (401, 403, 408, 429):
        # 401/403/408/429 say nothing about the request itself. (The seal's
        # `answer_from_seal` keeps a 403 REFUSED on purpose: there it is the
        # typed `snapshot_writer_forbidden`, and its caller treats REFUSED
        # and UNKNOWN alike, under `blocks_on_unknown()`.)
        return ExecutionAnswer(EXECUTION_REFUSED, reason=reason)
    return ExecutionAnswer(EXECUTION_UNKNOWN, reason=reason)


@dataclass(frozen=True)
class Identity:
    """This run's execution in the store, or why there is none.

    `execution_id`/`sequence` are the store's id and attempt, and `item`
    carries a ledger that contains them. `attached` is set when this run
    attached the execution itself and so owns advancing it to a terminal
    phase, even when the identity was then refused. `unknown` says whether a
    refusal is UNKNOWN (governed by `blocks_on_unknown()`) or a definite
    answer (governed by `new_answer_may_veto()`)."""

    execution_id: str = ""
    sequence: int = 0
    item: WorkItem | None = None
    attached: EngineRun | None = None
    refusal: str = ""
    unknown: bool = False
    note: str = ""
    #: `attach=False` (a dry run): nothing was sent, so there is no answer,
    #: neither a refusal nor an unknown.
    attach_skipped: bool = False


def _own(item: WorkItem, execution_id: str) -> ExecutionRef | None:
    return next((e for e in item.executions if e.execution_id == execution_id), None)


def resolve_identity(item: WorkItem, execution_id_flag: str | None, client: Any, *, attach: bool = True) -> Identity:
    """The execution this run is, from the store.

    - A `we_...` flag was created by the work-item layer (e.g. the resume
      route): it must be in this item's ledger, and it is used as-is. This
      branch reads only; it writes nothing whatever `attach` says.
    - Otherwise this run attaches its own engine run as Running. Any other
      flag (e.g. the dev_loop's seed hash) is correlation only.
    - `attach=False` (a dry run): nothing is sent, and there is no identity.
    - No engine run: no identity, never a local one."""
    if is_store_execution(execution_id_flag):
        own = _own(item, str(execution_id_flag))
        if own is None:
            return Identity(
                refusal=f"--execution-id {execution_id_flag} is not an execution of work item {item.work_item_id}",
            )
        return Identity(execution_id=own.execution_id, sequence=own.sequence, item=item)
    note = (
        f"--execution-id {execution_id_flag} is not a store execution; kept as correlation only"
        if execution_id_flag else ""
    )
    if not attach:
        return Identity(refusal="no execution identity: attach skipped", note=note, attach_skipped=True)
    run, why = engine_ref_from_env()
    if run is None:
        return Identity(refusal=f"no execution identity: {why}", note=note)
    answer = client.attach_execution(item.work_item_id, run, PHASE_RUNNING)
    if not answer.usable:
        return Identity(
            refusal=f"attach {run.engine}/{run.engine_ref}: {answer.verdict} {answer.reason}".strip(),
            unknown=answer.verdict != EXECUTION_REFUSED,
            note=note,
        )
    # From here on this run holds an execution it attached, so it owns
    # advancing it to a terminal phase whatever happens next.
    attached = run
    own = _own(item, answer.execution_id)
    if own is not None and own.sequence == answer.attempt:
        # A retry of this engine run: the ledger already holds it.
        return Identity(answer.execution_id, answer.attempt, item, attached=attached, note=note)
    if own is None and answer.verdict == EXECUTION_ATTACHED and answer.attempt == len(item.executions) + 1:
        # Created just now: mctl-api numbers it len(ledger)+1, so the ledger
        # read before the attach plus this entry is the whole ledger.
        ledger = (*item.executions, ExecutionRef(execution_id=answer.execution_id, sequence=answer.attempt))
        return Identity(
            answer.execution_id, answer.attempt, with_executions(item, ledger), attached=attached, note=note,
        )
    return Identity(
        attached=attached,
        refusal=(
            f"attached {answer.execution_id} (attempt {answer.attempt}), which the ledger read before the "
            f"attach does not account for; read again"
        ),
        unknown=True,
        note=note,
    )
