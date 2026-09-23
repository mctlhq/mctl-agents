"""Typed client-side mirror of the mctl-api `WorkItem` contract (mctl-api#227),
as mctl-api serves it: `workitem/v1` (docs/work-context-contract.md there,
mctl-api#349; mirror corrected in mctlhq/mctl-agents#452).

Frozen dataclasses, stdlib only, mirroring `orchestrator/lifecycle/contract.py`
in structure and in discipline: `from_payload` staticmethods that ignore keys
they do not recognise (an mctl-api deploy that adds a field must not become an
agents outage) and return `None` on a payload missing a required key (rather
than parsing into an all-empty record that reads downstream as a confident
answer). Every dataclass field is defaulted, so a value recorded before a
field existed still deserializes out of Temporal workflow history — the same
reason `orchestrator/lifecycle/contract.py`'s dataclasses are defaulted.

This module owns no state and performs no I/O; the transport lives in
`client.py`, and the rollout switch lives in `rollout.py`. mctl-api (#227)
owns the durable `WorkItem` row; this repo only mirrors it, tolerantly, as a
client (see docs/adr/011-work-item-resume-contract.md).

Closed vocabularies (`SURFACE_KINDS`, `ACTOR_KINDS`, `WORK_ITEM_STATES`) are
frozensets, exactly like `orchestrator/lifecycle/contract.py`'s
`HOLDING_STATES`/`FREE_STATES`: a value outside the set is never guessed
toward a permissive default, it classifies as `WORK_ITEM_UNKNOWN`.
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

# --- closed vocabularies ----------------------------------------------------

#: The only payload label this mirror reads. mctl-api ships a breaking change
#: as a new label, never as a silent change to this one, so any other value —
#: or none — is WORK_ITEM_UNKNOWN, never a best-effort parse.
SCHEMA_VERSION = "workitem/v1"

SURFACE_KINDS = frozenset({"github", "telegram", "web", "cli"})
ACTOR_KINDS = frozenset({"human", "agent", "system"})
# mctl-api's `workitems.State*` constants. `resumed` is deliberately absent:
# mctl-api records it only as an event kind, never as a state.
WORK_ITEM_STATES = frozenset({"active", "waiting", "completed", "superseded", "archived"})

#: States a reconstructed canonical state may VETO a run on, at rollout mode
#: `enforce` and above (never license one the issue path would refuse — see
#: rollout.py's `new_answer_may_veto`). Exactly mctl-api's terminal states:
#: no transition leaves them (`workitems.IsTerminal`).
TERMINAL_WORK_ITEM_STATES = frozenset({"completed", "superseded", "archived"})

# mctl-api's execution engines (`workitems.Engine*`). The engine decides
# what `engine_ref` means, so an unknown one refuses the entry. The phase
# is not checked: nothing here reads it, and a new mctl-api phase must not
# turn every read of a work item into UNKNOWN.
EXECUTION_ENGINES = frozenset({"temporal", "argo"})

# The error code mctl-api answers a missing (or invisible) work item with.
# Only this 404 is ABSENT; any other 404 did not come from the work-items
# handler.
NOT_FOUND_CODE = "work_item_not_found"

# `external_key` is a free dedupe key; the contract's own example is a GitHub
# issue URL, and only a value of that shape is read as `issue_url`. The same
# definition as `run_issue_investigator._ISSUE_URL_RE` (http(s), optional
# trailing slash), so the two never disagree about what an issue URL is.
_ISSUE_URL_RE = re.compile(r"^https?://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/issues/\d+/?$")

# The four answers to "does this WorkItem exist, and is it usable" — the same
# shape as orchestrator/lifecycle/contract.py's OWNED_BY_OTHER/OWNED_BY_ME/
# UNOWNED/UNKNOWN vocabulary, and UNKNOWN is the whole point: an unreachable
# store, a malformed body, or an unrecognised vocabulary value must never
# collapse into "absent" or "found" — see `work_item_verdict_for`.
WORK_ITEM_FOUND = "work-item-found"
WORK_ITEM_ABSENT = "work-item-absent"
WORK_ITEM_CONFLICT = "work-item-conflict"
WORK_ITEM_UNKNOWN = "work-item-unknown"


def _str(value: Any) -> str:
    return str(value) if isinstance(value, str) else ""


@dataclass(frozen=True)
class SurfaceRef:
    """Which surface an execution ran on. `kind` is closed vocabulary
    (`SURFACE_KINDS`); an unrecognised value is carried here as-is (parsing
    never rejects it) and only classified as WORK_ITEM_UNKNOWN at the
    verdict step, mirroring `Ownership.from_payload`'s "uncertainty is a
    value" discipline."""

    kind: str = ""
    surface_id: str = ""
    thread_ref: str = ""

    @staticmethod
    def from_payload(data: Any) -> SurfaceRef | None:
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        if kind is None or kind == "":
            # A kindless block is a legitimately ABSENT surface — an
            # execution predating the field, or a record serialized from a
            # default SurfaceRef() (dev_loop's own seed) — not a malformed
            # payload. Returning None here would make one such execution
            # abort the whole WorkItem parse into WORK_ITEM_UNKNOWN, the
            # exact intolerance work_item_verdict_for's non-empty-only rule
            # exists to avoid.
            return SurfaceRef(
                surface_id=_str(data.get("surface_id")), thread_ref=_str(data.get("thread_ref"))
            )
        if not isinstance(kind, str):
            return None
        return SurfaceRef(kind=kind, surface_id=_str(data.get("surface_id")), thread_ref=_str(data.get("thread_ref")))


@dataclass(frozen=True)
class ActorRef:
    """Who acted — a human, an agent, or the system itself. `kind` is closed
    vocabulary (`ACTOR_KINDS`)."""

    kind: str = ""
    actor_id: str = ""

    @staticmethod
    def from_payload(data: Any) -> ActorRef | None:
        if not isinstance(data, dict):
            return None
        kind = data.get("kind")
        if kind is None or kind == "":
            # Same tolerance as SurfaceRef.from_payload: a kindless block is
            # an absent actor, not a malformed payload.
            return ActorRef(actor_id=_str(data.get("actor_id")))
        if not isinstance(kind, str):
            return None
        return ActorRef(kind=kind, actor_id=_str(data.get("actor_id")))


@dataclass(frozen=True)
class WorkItemRef:
    """The durable identity a caller resumes against. `revision` is
    informational (optimistic-concurrency is mctl-api#227's job, not this
    repo's — see the "Out of scope" section of requirements.md)."""

    work_item_id: str = ""
    revision: str = ""

    @staticmethod
    def from_payload(data: Any) -> WorkItemRef | None:
        if not isinstance(data, dict):
            return None
        work_item_id = data.get("work_item_id")
        if not isinstance(work_item_id, str) or not work_item_id:
            return None
        return WorkItemRef(work_item_id=work_item_id, revision=_str(data.get("revision")))


@dataclass(frozen=True)
class ExecutionRef:
    """One execution of a work item — sibling correlation, never chaining
    (that is `ContextSnapshot.StepRef`'s job within one execution).

    `surface_transition` is not part of the mctl-api-owned shape (it is not
    listed in mctl-api#227); it is the dev-loop workflow's own local record
    of whether THIS execution's acceptance changed the surface or actor
    relative to the one before it, exposed on the `work_context` query for a
    trace view. `from_payload` tolerates its absence (defaults False), so a
    server-sent record without the key still parses.
    """

    execution_id: str = ""
    sequence: int = 0
    temporal_workflow_id: str = ""
    started_at: str = ""
    surface: SurfaceRef = field(default_factory=SurfaceRef)
    actor: ActorRef = field(default_factory=ActorRef)
    surface_transition: bool = False

    @staticmethod
    def from_payload(data: Any) -> ExecutionRef | None:
        if not isinstance(data, dict):
            return None
        execution_id = data.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            return None
        sequence_raw = data.get("sequence")
        if not isinstance(sequence_raw, int) or isinstance(sequence_raw, bool):
            return None
        sequence = sequence_raw

        surface_raw = data.get("surface")
        surface = SurfaceRef.from_payload(surface_raw) if surface_raw is not None else SurfaceRef()
        if surface_raw is not None and surface is None:
            # PRESENT but malformed is a malformed payload; ABSENT is simply
            # an execution predating this field (or one from a surface that
            # never reported one).
            return None

        actor_raw = data.get("actor")
        actor = ActorRef.from_payload(actor_raw) if actor_raw is not None else ActorRef()
        if actor_raw is not None and actor is None:
            return None

        return ExecutionRef(
            execution_id=execution_id,
            sequence=sequence,
            temporal_workflow_id=_str(data.get("temporal_workflow_id")),
            started_at=_str(data.get("started_at")),
            surface=surface or SurfaceRef(),
            actor=actor or ActorRef(),
            surface_transition=bool(data.get("surface_transition", False)),
        )

    @staticmethod
    def from_v1(data: Any, work_item_id: str) -> ExecutionRef | None:
        """One entry of mctl-api's `GET /api/v1/work-items/{id}/executions`
        listing (`workitems.Execution`): `id` -> `execution_id`, `attempt`
        -> `sequence`, and `engine_ref` -> `temporal_workflow_id` for a
        Temporal execution. `from_payload` stays the dev-loop's own local
        shape (workflow history and the `resume` signal).

        None on anything that does not describe an execution of THIS work
        item: a missing id, a non-positive attempt, another work item's id,
        or an engine outside mctl-api's closed vocabulary."""
        if not isinstance(data, dict):
            return None
        execution_id = data.get("id")
        if not isinstance(execution_id, str) or not execution_id:
            return None
        if data.get("work_item_id") != work_item_id:
            return None
        attempt = data.get("attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            return None
        engine = data.get("engine")
        if engine not in EXECUTION_ENGINES:
            return None
        return ExecutionRef(
            execution_id=execution_id,
            sequence=attempt,
            temporal_workflow_id=_str(data.get("engine_ref")) if engine == "temporal" else "",
            started_at=_str(data.get("started_at")),
        )


@dataclass(frozen=True)
class WorkItem:
    """The canonical durable task. `state` is closed vocabulary
    (`WORK_ITEM_STATES`).

    `from_payload` reads mctl-api's `work_item` object (`workitems.WorkItem`):
    `id` -> `work_item_id`, `state_version` -> `state_version` and, as a
    string, `revision`; `origin_surface` -> `origin.kind`; `external_key` ->
    `issue_url` only when it is a GitHub issue URL. `executions` is not part
    of that object: the client fills it from the executions route.
    `service` and `slug` are not part of `workitem/v1` and stay empty for a
    record read from mctl-api.
    """

    work_item_id: str = ""
    revision: str = ""
    state: str = ""
    origin: SurfaceRef = field(default_factory=SurfaceRef)
    executions: tuple[ExecutionRef, ...] = ()
    issue_url: str = ""
    service: str = ""
    slug: str = ""
    state_version: int = 0
    schema_version: str = ""
    external_key: str = ""

    @staticmethod
    def from_payload(data: Any) -> WorkItem | None:
        if not isinstance(data, dict):
            return None
        work_item_id = data.get("id")
        state = data.get("state")
        state_version = data.get("state_version")
        # A record with no id, state or state_version is not a record —
        # every real response carries all three, and this is the cheapest
        # way to tell a WorkItem from an error envelope that happened to be
        # 200 (mirroring Ownership.from_payload's phase/owner/state check).
        if not isinstance(work_item_id, str) or not work_item_id:
            return None
        if not isinstance(state, str) or not state:
            return None
        if not isinstance(state_version, int) or isinstance(state_version, bool) or state_version < 1:
            return None
        origin_surface = data.get("origin_surface")
        if origin_surface is None:
            origin_surface = ""  # absent or null: no origin recorded
        elif not isinstance(origin_surface, str):
            return None
        external_key = _str(data.get("external_key"))
        return WorkItem(
            work_item_id=work_item_id,
            revision=str(state_version),
            state=state,
            origin=SurfaceRef(kind=origin_surface),
            issue_url=external_key if _ISSUE_URL_RE.match(external_key) else "",
            state_version=state_version,
            schema_version=_str(data.get("schema_version")),
            external_key=external_key,
        )


def work_item_verdict_for(item: WorkItem) -> str:
    """Classify a parsed `WorkItem`: `WORK_ITEM_FOUND` unless `item.state` or
    any recorded execution's `surface.kind`/`actor.kind` carries a value
    outside this image's closed vocabulary, in which case
    `WORK_ITEM_UNKNOWN` — mirroring `orchestrator/lifecycle/contract.py`'s
    `verdict_for`, which never guesses an unrecognised value toward either
    side."""
    if item.state not in WORK_ITEM_STATES:
        return WORK_ITEM_UNKNOWN
    # `origin.kind` is held to the same vocabulary as execution surfaces:
    # `_work_context_ref` seals it as `origin_surface`, which
    # `ContextSnapshot.validate()` rejects when out of vocabulary — so an
    # unrecognised origin must be classified here, not crash the seal at
    # context mode `on`.
    if item.origin.kind and item.origin.kind not in SURFACE_KINDS:
        return WORK_ITEM_UNKNOWN
    # Only a NON-EMPTY kind can be out of vocabulary: `ExecutionRef.
    # from_payload` deliberately parses an absent surface/actor block into
    # `SurfaceRef()`/`ActorRef()` ("an execution predating this field, or
    # one from a surface that never reported one"), and dev_loop's own
    # execution #1 seed carries neither — the same non-empty-only rule
    # `ContextSnapshot.validate` applies to these fields.
    for execution in item.executions:
        if execution.surface.kind and execution.surface.kind not in SURFACE_KINDS:
            return WORK_ITEM_UNKNOWN
        if execution.actor.kind and execution.actor.kind not in ACTOR_KINDS:
            return WORK_ITEM_UNKNOWN
    return WORK_ITEM_FOUND


def work_item_unknown_reason(item: WorkItem) -> str:
    """Name the actual cause `work_item_verdict_for` refused on — the state,
    or the first out-of-vocabulary execution field — so a blocked operator
    is pointed at the offending value, not at a valid state."""
    if item.state not in WORK_ITEM_STATES:
        return f"unrecognised work item state {item.state!r}"
    if item.origin.kind and item.origin.kind not in SURFACE_KINDS:
        return f"work item carries unrecognised origin surface kind {item.origin.kind!r}"
    for execution in item.executions:
        if execution.surface.kind and execution.surface.kind not in SURFACE_KINDS:
            return (
                f"execution {execution.execution_id!r} carries unrecognised "
                f"surface kind {execution.surface.kind!r}"
            )
        if execution.actor.kind and execution.actor.kind not in ACTOR_KINDS:
            return (
                f"execution {execution.execution_id!r} carries unrecognised "
                f"actor kind {execution.actor.kind!r}"
            )
    return "unrecognised work item shape"


@dataclass(frozen=True)
class WorkItemAnswer:
    """The result of asking mctl-api about a work item. `verdict` is one of
    the four `WORK_ITEM_*` constants above; `UNKNOWN` is what an unreachable
    or unconfigured store returns, and it is NOT `ABSENT` — a caller that
    treats them the same has reintroduced the defect
    `orchestrator/lifecycle/contract.py`'s `OwnershipAnswer` exists to
    remove."""

    verdict: str = WORK_ITEM_UNKNOWN
    item: WorkItem | None = None
    reason: str = ""
    accepted: bool = False


def envelope_of(payload: Any) -> tuple[WorkItem | None, str]:
    """The `WorkItem` in a `workitem/v1` view — `{schema_version,
    work_item, state_version, latest_execution}` — or None and the reason
    it is not one.

    Fail closed on every disagreement: a missing or other `schema_version`
    (on the envelope or the item), a missing item, or an envelope
    `state_version` that is not the item's own."""
    if not isinstance(payload, dict):
        return None, "response is not a JSON object"
    schema = payload.get("schema_version")
    if schema != SCHEMA_VERSION:
        return None, f"unsupported schema_version {schema!r}, want {SCHEMA_VERSION!r}"
    item = WorkItem.from_payload(payload.get("work_item"))
    if item is None:
        return None, "no work item record in the response"
    if item.schema_version != SCHEMA_VERSION:
        return None, f"work item carries schema_version {item.schema_version!r}, want {SCHEMA_VERSION!r}"
    envelope_version = payload.get("state_version")
    if type(envelope_version) is not int or envelope_version != item.state_version:
        return None, (
            f"envelope state_version {payload.get('state_version')!r} is not the "
            f"work item's {item.state_version!r}"
        )
    return item, ""


def record_of(payload: Any) -> WorkItem | None:
    """The `WorkItem` in a response body, or None — `envelope_of` without
    the reason."""
    return envelope_of(payload)[0]


def latest_execution_id_of(payload: Any) -> str:
    """The id of the view's `latest_execution`, or "" when it names none."""
    latest = payload.get("latest_execution") if isinstance(payload, dict) else None
    return _str(latest.get("id")) if isinstance(latest, dict) else ""


def executions_from(status: int, payload: Any, work_item_id: str) -> tuple[tuple[ExecutionRef, ...] | None, str]:
    """Parse one `GET /api/v1/work-items/{id}/executions` response into the
    work item's executions, oldest attempt first, or None and why not.

    All or nothing: one malformed entry, another item's execution, or a
    repeated id or attempt makes the whole ledger untrustworthy — silently
    dropping an entry would understate prior_execution_ids, which resume's
    idempotency depends on."""
    if not 200 <= status < 300:
        return None, f"executions: {_error_of(status, payload)}"
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        schema = payload.get("schema_version") if isinstance(payload, dict) else None
        return None, f"executions: unsupported schema_version {schema!r}, want {SCHEMA_VERSION!r}"
    raw = payload.get("executions")
    if not isinstance(raw, list):
        return None, "executions: no executions list in the response"
    parsed: list[ExecutionRef] = []
    for entry in raw:
        execution = ExecutionRef.from_v1(entry, work_item_id)
        if execution is None:
            return None, f"executions: malformed or foreign execution record for {work_item_id!r}"
        parsed.append(execution)
    if len({e.execution_id for e in parsed}) != len(parsed):
        return None, "executions: repeated execution id"
    ordered = tuple(sorted(parsed, key=lambda e: e.sequence))
    # mctl-api numbers attempts 1..n with no gaps (`len(execs)+1` on every
    # attach) and lists them all, unpaginated. Anything else — a repeat, a
    # gap, a truncated or paginated listing — is not the whole ledger.
    if [e.sequence for e in ordered] != list(range(1, len(ordered) + 1)):
        return None, "executions: attempts are not exactly 1..n; the ledger is incomplete or repeated"
    return ordered, ""


def with_executions(item: WorkItem, executions: tuple[ExecutionRef, ...]) -> WorkItem:
    """`item` with the executions read from the executions route."""
    return replace(item, executions=executions)


def _error_of(status: int, payload: Any) -> str:
    err = payload.get("error") if isinstance(payload, dict) else None
    return str(err or f"HTTP {status}")


def answer_from(status: int, payload: dict[str, Any], *, path: str = "", body_empty: bool = False) -> WorkItemAnswer:
    """Turn one HTTP response into a `WorkItemAnswer`. The only
    implementation — kept here, not in the transport, for the reason
    `orchestrator/lifecycle/contract.py`'s `answer_from` gives: a second
    transport (an async activity, should one ever exist) must not carry its
    own copy of what an HTTP response means."""
    if 200 <= status < 300:
        item, why = envelope_of(payload)
        if item is None:
            return WorkItemAnswer(
                verdict=WORK_ITEM_UNKNOWN,
                reason=f"{why} ({status} response)",
                accepted=not body_empty,
            )
        verdict = work_item_verdict_for(item)
        reason = "" if verdict != WORK_ITEM_UNKNOWN else work_item_unknown_reason(item)
        return WorkItemAnswer(verdict=verdict, item=item, reason=reason, accepted=True)
    if status == 404:
        # The only branch that dereferences `payload` directly — guard the
        # type first. `_HTTPResult` coerces to `{}` today, but `answer_from`
        # is a public module-level function whose contract is "uncertainty
        # is a value, never an exception", and that must hold for any future
        # transport too.
        if not isinstance(payload, dict):
            return WorkItemAnswer(
                verdict=WORK_ITEM_UNKNOWN, reason=f"404 with a non-mapping payload from {path or 'a read'}"
            )
        if payload.get("code") != NOT_FOUND_CODE:
            # Only the work-items handler's own typed 404 means "no such
            # work item" — see the identical guard in
            # orchestrator/lifecycle/contract.py. Answering ABSENT for any
            # other 404 would report a wrong route or an ingress rule as a
            # clean absence.
            return WorkItemAnswer(
                verdict=WORK_ITEM_UNKNOWN,
                reason=f"404 without code {NOT_FOUND_CODE!r} from {path or 'a read'}",
            )
        return WorkItemAnswer(verdict=WORK_ITEM_ABSENT, reason="no record")
    if status == 409:
        return WorkItemAnswer(verdict=WORK_ITEM_CONFLICT, item=record_of(payload), reason=_error_of(status, payload))
    # 412, 503, 5xx, 401/403, a transport-surfaced timeout — every one of
    # these means "I could not resolve the work item", which is UNKNOWN and
    # never ABSENT.
    return WorkItemAnswer(verdict=WORK_ITEM_UNKNOWN, reason=_error_of(status, payload))


def execution_id_for(work_item_id: str, sequence: int, attempt: str) -> str:
    """Deterministic execution identity: a pure sha256 of
    `"{work_item_id}|{sequence}|{attempt}"`, the same shape as
    `orchestrator/lifecycle/contract.py`'s `idempotency_key_for`
    (`lifecycle/contract.py:1013-1024`).

    No UUID fallback, and none is ever added: ADR-010 §8 forbids a random
    identity here for the same reason it forbids one for an idempotency key
    — a duplicated `resume` signal (a retried surface callback, a racing
    second delivery) must derive the SAME id so the workflow's own
    `_seen_execution_ids` dedupe catches it by construction, not by luck. A
    random id would make every retry a fork.
    """
    raw = f"{work_item_id}|{sequence}|{attempt}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanonicalState:
    """What `reconstruct_canonical_state` derives — structured fields only.
    No field here is, or could ever be, a transcript: every value is an id,
    a status word, a filename, or a tuple of one of those. This is the same
    defence `orchestrator/context_snapshot.py`'s `ContextSource` uses (no
    payload field exists, so none can be smuggled in) — see that module's
    docstring at `context_snapshot.py:277-281`.
    """

    work_item_id: str = ""
    state: str = ""
    service: str = ""
    slug: str = ""
    issue_url: str = ""
    prior_execution_ids: tuple[str, ...] = ()
    prior_status: str = ""
    artifacts_present: tuple[str, ...] = ()
    reconstructed_from: tuple[str, ...] = ()


# The proposal-directory triplet plus the status file — the same four names
# `orchestrator/run_issue_investigator.py`'s TRIPLET + STATUS_FILENAME name.
# Duplicated as strings (not imported) because that module pulls in `yaml`
# and other non-stdlib dependencies at import time, which this stdlib-only
# package must never do.
_ARTIFACT_NAMES = ("requirements.md", "design.md", "tasks.md", ".status.yaml")

# `.status.yaml` is a flat `key: value` document (see
# `run_issue_investigator.write_status_yaml`); this package has no YAML
# parser (stdlib only) and does not need one — reconstruction only ever
# reads the single `status:` scalar, never anything nested, so a plain
# regex is the whole parser this needs and adds no dependency.
_STATUS_LINE_RE = re.compile(r"""^status:\s*["']?([A-Za-z0-9_-]+)["']?\s*$""", re.MULTILINE)


def _read_prior_status(proposal_dir: Path | None) -> str:
    if proposal_dir is None:
        return ""
    status_path = proposal_dir / ".status.yaml"
    try:
        if not status_path.is_file():
            return ""
        text = status_path.read_text(encoding="utf-8")
    except OSError:
        return ""
    match = _STATUS_LINE_RE.search(text)
    return match.group(1) if match else ""


def reconstruct_canonical_state(
    item: WorkItem,
    proposal_dir: Path | None,
    prior_digests: Sequence[Mapping[str, Any]],
) -> CanonicalState:
    """Rebuild canonical task state from structured fields alone.

    The signature exposes exactly three parameters and NONE of them can
    carry a conversation transcript: `item` is a `WorkItem` (ids, a state
    word, nested refs — see above), `proposal_dir` is a filesystem `Path`
    this function only ever uses to check for the presence of the four
    named gitops artifacts and to read one scalar out of `.status.yaml`,
    and `prior_digests` is read only for `ContextSnapshot.to_log_dict()`-
    shaped mappings (ids, counts, hashes — never a payload; see
    `context_snapshot.py`'s own `to_log_dict` docstring). There is no
    fourth parameter a caller could repurpose to pass in message history.
    """
    reconstructed_from: list[str] = ["work_item"]

    artifacts_present: tuple[str, ...] = ()
    prior_status = ""
    if proposal_dir is not None:
        artifacts_present = tuple(name for name in _ARTIFACT_NAMES if (proposal_dir / name).is_file())
        if artifacts_present:
            reconstructed_from.append("proposal_dir")
        prior_status = _read_prior_status(proposal_dir)

    # The store's own execution ledger is the primary source of sibling
    # correlation: deriving priors only from `prior_digests` reported every
    # production run as execution #1 of a work item that may have run many
    # times (the caller passes `()` today). Digests remain an ADDITIONAL
    # source — a snapshot digest can name an execution the store has not
    # recorded (or not yet).
    seen: dict[str, None] = {}
    for execution in sorted(item.executions, key=lambda e: e.sequence):
        if execution.execution_id:
            seen.setdefault(execution.execution_id, None)
    for digest in prior_digests:
        if isinstance(digest, Mapping) and digest.get("execution_id"):
            seen.setdefault(str(digest["execution_id"]), None)
    prior_execution_ids = tuple(seen)
    if prior_digests:
        reconstructed_from.append("prior_digests")

    return CanonicalState(
        work_item_id=item.work_item_id,
        state=item.state,
        service=item.service,
        slug=item.slug,
        issue_url=item.issue_url,
        prior_execution_ids=prior_execution_ids,
        prior_status=prior_status,
        artifacts_present=artifacts_present,
        reconstructed_from=tuple(reconstructed_from),
    )


def work_context_params(item: WorkItem, execution: ExecutionRef) -> dict[str, str]:
    """The `investigate_params` entries a resume-aware submission would add,
    as a pure `dict[str, str]` — never called from `dev_loop.py` today.

    `investigate_params` is POSTed verbatim as the operation body
    (`orchestrator/temporal/activities/argo.py:118-125`), and the mctl-api
    operation registry rejects unknown parameters until mctlhq/mctl-api#335
    and the CWFT change mctlhq/mctl-gitops#1279 land in their own repos. Per
    the "Submission-path wiring" open question in requirements.md, this
    helper exists and is unit-tested on its own, with no call site inside
    the workflow: `DevLoopWorkflow.run` reads no environment variable
    (rollout mode is read only inside activities elsewhere in this
    codebase, never inside `@workflow.defn` code, to keep every workflow
    command deterministic under replay), so gating a merge into
    `investigate_params` on `rollout.at_least(ENFORCE)` from inside the
    workflow itself would be the first exception to that rule. Wiring this
    in is a one-line addition once the cross-repo prerequisites land; until
    then the seam is exercised end-to-end by this function's own tests
    without touching workflow determinism.
    """
    return {
        "work_item_id": item.work_item_id,
        "execution_id": execution.execution_id,
        "execution_sequence": str(execution.sequence),
    }
