"""Observe-stage comparison of the two ownership mechanisms.

This module DECIDES NOTHING. Its only output is a log line, and the only way
it can affect a sweep is by raising — which is why every entry point here is
total. The classification table is mctl-api's
``internal/lifecycle/diverge.go``; the two must agree, because a divergence
counted differently in two places is a divergence nobody can explain
afterwards.

What is being compared:

- the OLD mechanism — ``run_shepherd._dev_loop_owns_answer``: is a live
  ``DevLoopWorkflow`` driving this entity?
- the NEW mechanism — the ownership store, read through ``OwnershipClient``.

The old mechanism asks about a PROPOSAL and the store keys on the PULL
REQUEST. They are bridged by ``pr_url`` at discovery, which every non-reconcile
ref already carries; see ``compare_proposal_refs``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from orchestrator.lifecycle.client import OwnershipClient
from orchestrator.lifecycle.contract import (
    KIND_PULL_REQUEST,
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    OWNER_DEVLOOP_WORKFLOW,
    PHASE_REVIEW_REMEDIATION,
    UNKNOWN,
    WROTE_NO_RECORD,
    EntityRef,
    OwnershipAnswer,
)

# --- the classification vocabulary -------------------------------------
#
# These six strings are copied from mctl-api's internal/lifecycle/diverge.go
# (DivergeAgree, DivergeStorePermits, DivergeStoreForbids,
# DivergeOwnerMismatch, DivergeStoreUnknown, DivergeLegacyUnknown). CI cannot
# read across repositories, so a test pins them as literals naming that file —
# which makes this the weakest link in the "byte-for-byte" guarantee, and the
# one place to look first if the two counts ever disagree.

#: Both mechanisms permit, or both forbid.
DIVERGE_AGREE = "agree"

#: The store would license an action the old mechanism forbids. THE ONLY
#: DANGEROUS CLASS: under enforce the shepherd would take a pull request
#: another machine is actively pushing to. One of these is a stop-the-soak
#: event, not a metric to watch trend downward.
DIVERGE_STORE_PERMITS = "store-permits-old-forbids"

#: The store withholds an action the old mechanism permits. Safe by
#: construction — it never licenses anything — and expected in bulk during the
#: soak (bootstrap residue, pr-steward rows the legacy check has no concept
#: of), which is why it is counted separately from the dangerous direction.
DIVERGE_STORE_FORBIDS = "store-forbids-old-permits"

#: Both say the entity is held and they name different holders. Not dangerous:
#: both stand down. Reported because it is the class that reveals a mis-wired
#: comparison — a compare that never disagreed is likelier broken than perfect.
DIVERGE_OWNER_MISMATCH = "owner-mismatch"

#: No answer from the store. NOT a divergence: an absent answer is not a
#: disagreement, and counting it as one would make an mctl-api outage look like
#: a correctness problem.
DIVERGE_STORE_UNKNOWN = "store-unknown"

#: No answer from the old mechanism. Also not a divergence, and separate from
#: the above so an operator can tell which side went quiet.
DIVERGE_LEGACY_UNKNOWN = "legacy-unknown"

#: Closed, and ordered as in diverge.go so the totals line is stable.
DIVERGENCE_CLASSES = (
    DIVERGE_AGREE,
    DIVERGE_STORE_PERMITS,
    DIVERGE_STORE_FORBIDS,
    DIVERGE_OWNER_MISMATCH,
    DIVERGE_STORE_UNKNOWN,
    DIVERGE_LEGACY_UNKNOWN,
)

DANGEROUS_CLASSES = frozenset({DIVERGE_STORE_PERMITS})

# --- the legacy answer -------------------------------------------------
#
# Three-valued, mirroring diverge.go's LegacyAnswer. `_dev_loop_owns` returns a
# bool and collapses "no" with "could not tell"; that collapse is what this
# whole contract exists to undo, and reintroducing it one layer up would make
# every fail-open path read as a measured disagreement.
LEGACY_UNKNOWN = "unknown"
LEGACY_OWNED = "owned"
LEGACY_FREE = "free"

#: Which legacy answer wins when two proposals map to one pull request.
#: Explicit, because letting a dict last-write-win would make the class depend
#: on the order the proposal directories happened to be listed in.
_LEGACY_PRECEDENCE = (LEGACY_OWNED, LEGACY_FREE, LEGACY_UNKNOWN)

#: Stable prefix. mctl-gitops matches one regex per class against it; the
#: class is in the COUNTER NAME because promtail's metrics stage attaches no
#: dynamic labels.
LOG_PREFIX = "lifecycle-shadow:"

#: The batched read's own timeout, spent after the sweep's 60s pool budget.
SHADOW_TIMEOUT_S = 10

_WHITESPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class Divergence:
    """One comparison.

    ``dangerous`` is a bool rather than a severity because there are exactly
    two consequences: stop the rollout, or write it down.
    """

    divergence_class: str = DIVERGE_STORE_UNKNOWN
    dangerous: bool = False
    detail: str = ""
    #: The store's verdict, carried for the log line only.
    store: str = UNKNOWN


@dataclass
class Totals:
    """Per-tick counts. Every class is present, zeros included: a class that
    only appears once it is non-zero is a class an operator cannot tell from
    one that was never wired up."""

    counts: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(DIVERGENCE_CLASSES, 0)
    )
    compared: int = 0

    def record(self, divergence: Divergence) -> None:
        self.compared += 1
        self.counts[divergence.divergence_class] = (
            self.counts.get(divergence.divergence_class, 0) + 1
        )


def merge_legacy(first: str, second: str) -> str:
    """The stronger of two legacy answers for one entity."""
    for candidate in _LEGACY_PRECEDENCE:
        if candidate in (first, second):
            return candidate
    return LEGACY_UNKNOWN


def held(answer: OwnershipAnswer) -> bool | None:
    """Does the store's record withhold the entity? None when it cannot say.

    Read from mctl-api's ``derived.held``, NOT recomputed and emphatically not
    taken from ``OwnershipAnswer.blocks_others``: that property is True for
    UNKNOWN — correctly, because uncertainty must resolve toward "do not act" —
    and folding an absent answer into "held" here would report an mctl-api
    outage as agreement.

    None on a present record means the server predates the derived block. The
    caller reports store-unknown, which is loud; the alternative is to
    re-derive the takeover predicate in a second language, and mctl-api's
    derive.go records that the first consumer to try got it wrong.
    """
    if answer.verdict not in (OWNED_BY_OTHER, OWNED_BY_ME):
        # UNOWNED: a real answer, and a real "nobody holds this". Matches
        # Derive(nil), which reports Held false.
        return False
    if answer.ownership is None:
        return None
    return answer.ownership.held


def classify(answer: OwnershipAnswer, legacy: str) -> Divergence:
    """Compare the store's answer with the old mechanism's.

    Argument order and branch order both mirror diverge.go's Classify. The
    store-readable check comes FIRST: when both sides are quiet the answer is
    store-unknown, and swapping the two would report the same situation
    differently depending on which repository you read.
    """
    if answer.verdict in (UNKNOWN, WROTE_NO_RECORD):
        # WROTE_NO_RECORD cannot arise on a read; folded in rather than left to
        # fall through into held(), where it would be a silent mis-read.
        return Divergence(DIVERGE_STORE_UNKNOWN, detail=answer.reason, store=answer.verdict)

    is_held = held(answer)
    if is_held is None:
        return Divergence(
            DIVERGE_STORE_UNKNOWN,
            detail="record carries no derived.held; mctl-api predates the field",
            store=answer.verdict,
        )

    if legacy == LEGACY_UNKNOWN:
        return Divergence(DIVERGE_LEGACY_UNKNOWN, store=answer.verdict)

    owner = answer.ownership.owner if answer.ownership is not None else None
    status = answer.ownership.derived_status if answer.ownership is not None else "no record"

    if is_held and legacy == LEGACY_OWNED:
        if owner is not None and owner.type == OWNER_DEVLOOP_WORKFLOW:
            return Divergence(DIVERGE_AGREE, store=answer.verdict)
        return Divergence(
            DIVERGE_OWNER_MISMATCH,
            detail=(
                f"store names {owner.type}/{owner.id}; "
                "a live DevLoopWorkflow is driving the same entity"
                if owner is not None
                else "store holds the entity without naming an owner"
            ),
            store=answer.verdict,
        )
    if is_held and legacy == LEGACY_FREE:
        return Divergence(
            DIVERGE_STORE_FORBIDS,
            detail=(
                f"store names {owner.type}/{owner.id} ({status}); no live DevLoopWorkflow"
                if owner is not None
                else f"store holds the entity ({status}); no live DevLoopWorkflow"
            ),
            store=answer.verdict,
        )
    if not is_held and legacy == LEGACY_OWNED:
        return Divergence(
            DIVERGE_STORE_PERMITS,
            dangerous=True,
            detail=(
                f"store holds no live owner ({status}) while a "
                "DevLoopWorkflow is driving the entity"
            ),
            store=answer.verdict,
        )
    # not held and the old mechanism stands down: both permit.
    return Divergence(DIVERGE_AGREE, store=answer.verdict)


def _one_line(text: str) -> str:
    """Collapse whitespace so one comparison is one log line.

    A newline inside `detail` would split a record across two lines, and
    promtail counts lines.
    """
    return _WHITESPACE.sub(" ", text).strip().replace('"', "'")


def log_line(entity_id: str, phase: str, legacy: str, divergence: Divergence) -> str:
    """One comparison, as the line mctl-gitops turns into a counter.

    `detail` is LAST and quoted so it can contain anything without breaking the
    match, and the class is followed by a space — the regexes anchor on that
    rather than on a word boundary, because `-` is a non-word character and
    `\\bstore-permits\\b` would also match a seventh class added later with the
    same prefix.
    """
    return (
        f"{LOG_PREFIX} divergence"
        f" class={divergence.divergence_class}"
        f" entity={entity_id}"
        f" phase={phase}"
        f" legacy={legacy}"
        f" store={divergence.store}"
        f" dangerous={'true' if divergence.dangerous else 'false'}"
        f' detail="{_one_line(divergence.detail)}"'
    )


def totals_line(totals: Totals) -> str:
    """The per-tick summary. Deliberately carries no `class=` token, so the
    per-class regexes cannot match it and double-count."""
    counted = " ".join(f"{name}={totals.counts.get(name, 0)}" for name in DIVERGENCE_CLASSES)
    return f"{LOG_PREFIX} tick totals compared={totals.compared} {counted}"


def _emit(line: str) -> None:
    # flush=True on every line: an Argo step pod killed at its budget must not
    # lose the comparisons it already made.
    print(line, flush=True)


def compare_entities(
    legacy_by_entity: dict[str, str],
    *,
    kind: str = KIND_PULL_REQUEST,
    phase: str = PHASE_REVIEW_REMEDIATION,
    client: OwnershipClient | None = None,
) -> Totals:
    """Read the store for these entities and log one comparison each.

    `client` is injectable because ``run_shepherd.MCTL_API_URL`` is bound at
    import time while ``client.api_base()`` reads the environment per call — a
    test that sets the env moves one and not the other.
    """
    totals = Totals()
    if not legacy_by_entity:
        return totals

    resolved = client if client is not None else OwnershipClient(timeout=SHADOW_TIMEOUT_S)
    ids = sorted(legacy_by_entity)
    # asking=None: the comparison needs only `held` and the owner type, and
    # OWNED_BY_ME and OWNED_BY_OTHER both mean held. Supplying an identity
    # would buy nothing and would manufacture spurious OWNED_BY_ME the moment
    # the shepherd started writing rows.
    answers = resolved.get_many(kind, phase, ids, asking=None)

    for entity_id in ids:
        legacy = legacy_by_entity[entity_id]
        answer = answers.get(entity_id) or OwnershipAnswer(
            verdict=UNKNOWN, reason="id absent from the batch response"
        )
        divergence = classify(answer, legacy)
        totals.record(divergence)
        _emit(log_line(entity_id, phase, legacy, divergence))

    _emit(totals_line(totals))
    return totals


def entity_id_for_pr(owner: str, repo: str, number: int) -> str:
    """The store's key for a pull request.

    ``EntityRef.for_pull_request`` takes "owner/repo" as ONE argument. Handing
    it the bare repo yields ids the store has never seen, every comparison
    reads store-unknown, and the soak looks healthy while measuring nothing —
    which is why this wrapper exists rather than the call being inlined.
    """
    return EntityRef.for_pull_request(f"{owner}/{repo}", number).id


def compare_proposal_refs(refs, legacy_by_index: dict[int, str], parse_pr_url) -> Totals:
    """The run_shepherd adapter: map proposal refs onto pull request ids.

    Total by contract — the sweep calls this for observation only, and the one
    way an observer can change a decision is by raising. The per-ref catch is
    deliberately broad: `parse_pr_url` is injected, and narrowing it to the
    exceptions today's implementation happens to raise would make this
    function's totality depend on a detail of its caller.
    """
    totals = Totals()
    legacy_by_entity: dict[str, str] = {}
    unmapped = 0

    for i, ref in enumerate(refs):
        legacy = legacy_by_index.get(i, LEGACY_UNKNOWN)
        try:
            owner, repo, number = parse_pr_url(getattr(ref, "pr_url", "") or "")
            entity_id = entity_id_for_pr(owner, repo, int(number))
        except Exception as exc:  # noqa: BLE001 — this function is total by contract
            # No new class: the vocabulary is closed and mirrors diverge.go.
            # The store genuinely gave no answer for this entity, so it counts
            # store-unknown and the totals still sum to len(refs).
            unmapped += 1
            divergence = Divergence(
                DIVERGE_STORE_UNKNOWN, detail=f"no pull request entity id: {exc}"
            )
            totals.record(divergence)
            _emit(log_line(getattr(ref, "slug", "?"), PHASE_REVIEW_REMEDIATION, legacy, divergence))
            continue
        if entity_id in legacy_by_entity:
            merged = merge_legacy(legacy_by_entity[entity_id], legacy)
            print(
                f"warn: {LOG_PREFIX} two proposals map to {entity_id}; "
                f"taking {merged!r}",
                flush=True,
            )
            legacy_by_entity[entity_id] = merged
        else:
            legacy_by_entity[entity_id] = legacy

    measured = compare_entities(legacy_by_entity)
    totals.compared += measured.compared
    for name, count in measured.counts.items():
        totals.counts[name] = totals.counts.get(name, 0) + count
    return totals


def enabled() -> bool:
    """Whether the shadow compare should run at all.

    Reads the rollout mode through ``rollout.computes_new_answer``; the import
    is local so this module stays importable in a context where the rollout
    module's environment is not set up.
    """
    from orchestrator.lifecycle import rollout

    return rollout.computes_new_answer()

