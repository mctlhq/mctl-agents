"""The shadow compare: does it classify the way mctl-api does?

Every constant and every cell here is transcribed from
``mctl-api/internal/lifecycle/diverge.go``. CI cannot read across
repositories, so the transcription is pinned as literals rather than derived —
which makes THIS FILE the weakest link in the "byte-for-byte" guarantee, and
the first place to look if the two sides ever report different totals.
"""
from __future__ import annotations

import re

import pytest

from orchestrator.lifecycle import shadow
from orchestrator.lifecycle.contract import (
    OWNED_BY_ME,
    OWNED_BY_OTHER,
    UNKNOWN,
    UNOWNED,
    WROTE_NO_RECORD,
    EntityRef,
    Owner,
    Ownership,
    OwnershipAnswer,
)


def _record(owner_type: str = "devloop-workflow", *, held: bool | None = True,
            status: str = "healthy") -> Ownership:
    return Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type=owner_type, id=f"{owner_type}-1"),
        state="active",
        healthy=True,
        held=held,
        derived_status=status,
    )


def _answer(verdict: str, **kw) -> OwnershipAnswer:
    if verdict in (OWNED_BY_OTHER, OWNED_BY_ME):
        kw.setdefault("ownership", _record())
    return OwnershipAnswer(verdict=verdict, **kw)


# --- the vocabulary ----------------------------------------------------


def test_the_six_class_names_match_diverge_go() -> None:
    """Pinned as literals against mctl-api/internal/lifecycle/diverge.go.

    diverge.go constant  -> the literal below
      DivergeAgree         "agree"
      DivergeStorePermits  "store-permits-old-forbids"
      DivergeStoreForbids  "store-forbids-old-permits"
      DivergeOwnerMismatch "owner-mismatch"
      DivergeStoreUnknown  "store-unknown"
      DivergeLegacyUnknown "legacy-unknown"
    """
    assert shadow.DIVERGE_AGREE == "agree"
    assert shadow.DIVERGE_STORE_PERMITS == "store-permits-old-forbids"
    assert shadow.DIVERGE_STORE_FORBIDS == "store-forbids-old-permits"
    assert shadow.DIVERGE_OWNER_MISMATCH == "owner-mismatch"
    assert shadow.DIVERGE_STORE_UNKNOWN == "store-unknown"
    assert shadow.DIVERGE_LEGACY_UNKNOWN == "legacy-unknown"
    assert shadow.DANGEROUS_CLASSES == frozenset({"store-permits-old-forbids"})
    # Closed and complete: a class that is not in the tuple gets no counter in
    # mctl-gitops and disappears from the totals line.
    assert len(shadow.DIVERGENCE_CLASSES) == 6
    assert len(set(shadow.DIVERGENCE_CLASSES)) == 6


# --- the truth table ---------------------------------------------------


@pytest.mark.parametrize(
    ("verdict", "owner_type", "legacy", "expected", "dangerous"),
    [
        # Both name the DevLoop.
        (OWNED_BY_OTHER, "devloop-workflow", shadow.LEGACY_OWNED, shadow.DIVERGE_AGREE, False),
        # Held by someone else while a live DevLoop drives it. Both stand
        # down, so it is not dangerous — but it is the class that reveals a
        # mis-wired comparison, which is why the soak requires seeing one.
        (OWNED_BY_OTHER, "shepherd", shadow.LEGACY_OWNED, shadow.DIVERGE_OWNER_MISMATCH, False),
        (OWNED_BY_ME, "shepherd", shadow.LEGACY_OWNED, shadow.DIVERGE_OWNER_MISMATCH, False),
        # The store withholds what the old mechanism would sweep. Safe: it
        # never licenses anything.
        (OWNED_BY_OTHER, "shepherd", shadow.LEGACY_FREE, shadow.DIVERGE_STORE_FORBIDS, False),
        (OWNED_BY_OTHER, "devloop-workflow", shadow.LEGACY_FREE, shadow.DIVERGE_STORE_FORBIDS, False),
        # Both permit.
        (UNOWNED, "", shadow.LEGACY_FREE, shadow.DIVERGE_AGREE, False),
        # THE DANGEROUS CELL.
        (UNOWNED, "", shadow.LEGACY_OWNED, shadow.DIVERGE_STORE_PERMITS, True),
        # Neither side is a disagreement.
        (UNKNOWN, "", shadow.LEGACY_OWNED, shadow.DIVERGE_STORE_UNKNOWN, False),
        (UNKNOWN, "", shadow.LEGACY_FREE, shadow.DIVERGE_STORE_UNKNOWN, False),
        (UNOWNED, "", shadow.LEGACY_UNKNOWN, shadow.DIVERGE_LEGACY_UNKNOWN, False),
        (OWNED_BY_OTHER, "shepherd", shadow.LEGACY_UNKNOWN, shadow.DIVERGE_LEGACY_UNKNOWN, False),
    ],
)
def test_the_classification_table(verdict, owner_type, legacy, expected, dangerous) -> None:
    answer = (
        OwnershipAnswer(verdict=verdict, ownership=_record(owner_type))
        if owner_type
        else OwnershipAnswer(verdict=verdict)
    )
    got = shadow.classify(answer, legacy)
    assert got.divergence_class == expected
    assert got.dangerous is dangerous


def test_only_one_class_is_ever_dangerous() -> None:
    """A second dangerous class would change what the soak's stop condition
    means, and the alert in mctl-gitops watches exactly one counter."""
    for verdict in (OWNED_BY_OTHER, OWNED_BY_ME, UNOWNED, UNKNOWN):
        for legacy in (shadow.LEGACY_OWNED, shadow.LEGACY_FREE, shadow.LEGACY_UNKNOWN):
            answer = _answer(verdict)
            got = shadow.classify(answer, legacy)
            assert got.dangerous == (got.divergence_class in shadow.DANGEROUS_CLASSES)


# --- the traps ---------------------------------------------------------


def test_a_dead_handoff_is_not_held() -> None:
    """The exact trap diverge.go:113-118 documents.

    A handing-off row past its liveness bound reports the more specific status
    `handoff-stalled` while being dead and recoverable. Classifying off the
    status string reports it as withheld — and turns the ONE dangerous class
    into `agree`. mctl-api answers held=False for it; this asserts the Python
    side takes that answer rather than second-guessing it from the status.
    """
    answer = OwnershipAnswer(
        verdict=OWNED_BY_OTHER,
        ownership=_record("devloop-workflow", held=False, status="handoff-stalled"),
    )
    got = shadow.classify(answer, shadow.LEGACY_OWNED)
    assert got.divergence_class == shadow.DIVERGE_STORE_PERMITS
    assert got.dangerous is True


def test_unknown_is_never_read_as_held() -> None:
    """OwnershipAnswer.blocks_others is True for UNKNOWN -- correctly, since
    uncertainty must resolve toward "do not act". Using it as `held` would
    fold an mctl-api outage into store-forbids and report agreement the soak
    never measured."""
    answer = OwnershipAnswer(verdict=UNKNOWN, reason="connection refused")
    assert answer.blocks_others is True
    assert shadow.held(answer) is False
    assert shadow.classify(answer, shadow.LEGACY_FREE).divergence_class == shadow.DIVERGE_STORE_UNKNOWN


def test_store_unknown_is_checked_before_legacy_unknown() -> None:
    """Classify checks storeReadable before legacy == LegacyUnknown, so when
    both sides are quiet the answer is store-unknown. Swapping the two would
    report the same situation differently in the two repositories."""
    got = shadow.classify(OwnershipAnswer(verdict=UNKNOWN), shadow.LEGACY_UNKNOWN)
    assert got.divergence_class == shadow.DIVERGE_STORE_UNKNOWN


def test_a_record_without_derived_held_is_store_unknown() -> None:
    """An mctl-api that predates the derived block cannot answer the takeover
    predicate. Absent is reported as absent, loudly: the alternative is to
    re-derive it here, in a second language, which is the mistake derive.go
    records the first consumer making."""
    answer = OwnershipAnswer(verdict=OWNED_BY_OTHER, ownership=_record(held=None))
    got = shadow.classify(answer, shadow.LEGACY_OWNED)
    assert got.divergence_class == shadow.DIVERGE_STORE_UNKNOWN
    assert "derived.held" in got.detail


def test_wrote_no_record_cannot_fall_through() -> None:
    """Unreachable on a read, folded in rather than left to reach held()."""
    got = shadow.classify(OwnershipAnswer(verdict=WROTE_NO_RECORD), shadow.LEGACY_FREE)
    assert got.divergence_class == shadow.DIVERGE_STORE_UNKNOWN


def test_unowned_is_a_real_answer_not_an_absence() -> None:
    assert shadow.held(OwnershipAnswer(verdict=UNOWNED)) is False


# --- the log lines -----------------------------------------------------


def _promtail_regexes() -> dict[str, re.Pattern[str]]:
    """The regexes the mctl-gitops promtail stage will carry.

    Anchored on the trailing SPACE after the class, not on a word boundary:
    `-` is a non-word character, so `\\bstore-permits-old-forbids\\b` would also
    match a seventh class that merely started with the same text.
    """
    return {
        name: re.compile(r"lifecycle-shadow: divergence class=" + re.escape(name) + r" ")
        for name in shadow.DIVERGENCE_CLASSES
    }


@pytest.mark.parametrize("name", shadow.DIVERGENCE_CLASSES)
def test_each_line_matches_exactly_one_counter_regex(name) -> None:
    line = shadow.log_line(
        "mctlhq/mctl-web#42", "review-remediation", shadow.LEGACY_FREE,
        shadow.Divergence(name, dangerous=name in shadow.DANGEROUS_CLASSES,
                          detail="store names shepherd/shepherd:mctl-web (healthy)"),
    )
    matched = [other for other, rx in _promtail_regexes().items() if rx.search(line)]
    assert matched == [name], f"{line!r} matched {matched}"


def test_the_totals_line_matches_no_counter_regex() -> None:
    """It carries no `class=` token on purpose. If it did, every tick would
    count twice: once per comparison and once in the summary."""
    totals = shadow.Totals()
    for name in shadow.DIVERGENCE_CLASSES:
        totals.record(shadow.Divergence(name))
    line = shadow.totals_line(totals)
    assert not [n for n, rx in _promtail_regexes().items() if rx.search(line)]
    assert "compared=6" in line
    # Every class present, zeros included -- a class that only appears when
    # non-zero cannot be told from one that was never wired up.
    for name in shadow.DIVERGENCE_CLASSES:
        assert f"{name}=" in line


def test_a_multiline_detail_stays_one_line() -> None:
    """promtail counts lines. A newline in `detail` would split one comparison
    into two, and the second half would match nothing."""
    line = shadow.log_line(
        "mctlhq/mctl-web#42", "review-remediation", shadow.LEGACY_OWNED,
        shadow.Divergence(shadow.DIVERGE_STORE_UNKNOWN, detail="boom\nand a second line"),
    )
    assert "\n" not in line


# --- compare_entities --------------------------------------------------


class _FakeClient:
    def __init__(self, answers=None, raises=None):
        self._answers = answers or {}
        self._raises = raises
        self.calls: list[tuple] = []

    def get_many(self, kind, phase, ids, asking=None):
        self.calls.append((kind, phase, tuple(ids), asking))
        if self._raises is not None:
            raise self._raises
        return {i: self._answers.get(i, OwnershipAnswer(verdict=UNOWNED)) for i in ids}


def test_compare_entities_asks_without_an_identity() -> None:
    """asking=None: the comparison needs only `held` and the owner type, and a
    fake identity would manufacture spurious OWNED_BY_ME."""
    client = _FakeClient()
    shadow.compare_entities({"mctlhq/mctl-web#42": shadow.LEGACY_FREE}, client=client)
    assert client.calls[0][3] is None


def test_compare_entities_counts_every_class(capsys) -> None:
    client = _FakeClient({
        "a#1": OwnershipAnswer(verdict=UNOWNED),
        "a#2": OwnershipAnswer(verdict=UNKNOWN, reason="down"),
    })
    totals = shadow.compare_entities(
        {"a#1": shadow.LEGACY_OWNED, "a#2": shadow.LEGACY_FREE}, client=client
    )
    assert totals.compared == 2
    assert totals.counts[shadow.DIVERGE_STORE_PERMITS] == 1
    assert totals.counts[shadow.DIVERGE_STORE_UNKNOWN] == 1
    assert totals.counts[shadow.DIVERGE_AGREE] == 0
    out = capsys.readouterr().out
    assert out.count("lifecycle-shadow: divergence") == 2
    assert out.count("lifecycle-shadow: tick totals") == 1


def test_an_id_missing_from_the_batch_is_unknown_not_unowned(capsys) -> None:
    """The one wrong answer that licenses action."""

    class _Partial(_FakeClient):
        def get_many(self, kind, phase, ids, asking=None):
            return {}

    totals = shadow.compare_entities({"a#1": shadow.LEGACY_OWNED}, client=_Partial())
    assert totals.counts[shadow.DIVERGE_STORE_UNKNOWN] == 1
    assert totals.counts[shadow.DIVERGE_STORE_PERMITS] == 0


def test_merge_legacy_prefers_the_stronger_answer() -> None:
    """Two proposals can point at one pull request. An explicit rule, because
    last-write-wins would make the class depend on directory ordering."""
    assert shadow.merge_legacy(shadow.LEGACY_FREE, shadow.LEGACY_OWNED) == shadow.LEGACY_OWNED
    assert shadow.merge_legacy(shadow.LEGACY_UNKNOWN, shadow.LEGACY_FREE) == shadow.LEGACY_FREE
    assert shadow.merge_legacy(shadow.LEGACY_UNKNOWN, shadow.LEGACY_UNKNOWN) == shadow.LEGACY_UNKNOWN


def test_entity_id_takes_owner_slash_repo_as_one_argument() -> None:
    """EntityRef.for_pull_request's first argument is "owner/repo". Handing it
    the bare repo yields ids the store has never seen -- every comparison reads
    store-unknown and the soak looks healthy while measuring nothing."""
    assert shadow.entity_id_for_pr("mctlhq", "mctl-web", 42) == "mctlhq/mctl-web#42"


class _Ref:
    def __init__(self, pr_url: str, slug: str = "issue-1-x"):
        self.pr_url = pr_url
        self.slug = slug


def _parse(pr_url: str):
    """Mirrors run_shepherd._parse_pr_url, including its ValueError."""
    parts = pr_url.rstrip("/").split("/")
    try:
        return parts[-4], parts[-3], int(parts[-1])
    except (ValueError, IndexError) as exc:
        raise ValueError(f"Cannot parse PR URL: {pr_url!r}") from exc


def test_compare_proposal_refs_maps_to_pull_request_ids() -> None:
    """The ids the store is actually asked about.

    EntityRef.for_pull_request takes "owner/repo" as ONE argument; handing it
    the bare repo yields ids the store has never seen, every comparison reads
    store-unknown, and the soak looks healthy while measuring nothing. Asserted
    against the client rather than against a substituted compare_entities, so
    the real mapping path runs.
    """
    client = _FakeClient()
    shadow.compare_proposal_refs(
        [_Ref("https://github.com/mctlhq/mctl-web/pull/42")],
        {0: shadow.LEGACY_OWNED},
        _parse,
        client=client,
    )
    assert [call[2] for call in client.calls] == [("mctlhq/mctl-web#42",)]


def test_exactly_one_totals_line_per_tick(capsys) -> None:
    """The per-class counters and the summary are read together, and the log
    format depends on there being one summary per tick. A partial tick that
    emitted divergence lines and no summary is how the two drift apart."""
    shadow.compare_proposal_refs(
        [_Ref("https://github.com/mctlhq/mctl-web/pull/42"), _Ref("not-a-url", slug="issue-2-x")],
        {0: shadow.LEGACY_FREE, 1: shadow.LEGACY_FREE},
        _parse,
        client=_FakeClient(),
    )
    out = capsys.readouterr().out
    assert out.count("tick totals") == 1
    # The unmapped ref is folded into the SAME summary as the compared one --
    # not counted after it was already emitted.
    assert "compared=2" in out
    assert "store-unknown=1" in out


def test_a_failing_store_still_closes_the_tick(capsys) -> None:
    totals = shadow.compare_proposal_refs(
        [_Ref("https://github.com/mctlhq/mctl-web/pull/42")],
        {0: shadow.LEGACY_OWNED},
        _parse,
        client=_FakeClient(raises=RuntimeError("boom")),
    )
    out = capsys.readouterr().out
    assert out.count("tick totals") == 1
    assert "compare failed" in out
    assert totals.compared == 0


def test_compare_entities_is_bounded_in_wall_clock(monkeypatch, capsys) -> None:
    """An observer that delays the thing it observes has stopped being one.

    get_many chunks internally and issues its chunks sequentially, so without a
    budget the compare's cost grows with the sweep and can outlast the 5-minute
    cron interval. Ids past the budget are NOT COMPARED -- absent from the
    totals rather than counted store-unknown, because the store did not fail to
    answer, it was never asked.
    """
    monkeypatch.setattr(shadow, "SHADOW_CHUNK_SIZE", 1)
    monkeypatch.setattr(shadow, "SHADOW_BUDGET_S", 0)

    client = _FakeClient()
    totals = shadow.compare_entities(
        {f"mctlhq/mctl-web#{i}": shadow.LEGACY_FREE for i in range(5)}, client=client
    )
    # The budget can only stop the compare BETWEEN chunks, so the first chunk
    # always runs -- a budget that could skip everything would make a slow tick
    # indistinguishable from one that never happened.
    assert totals.compared == 1
    assert len(client.calls) == 1
    out = capsys.readouterr().out
    assert "not compared" in out
    assert out.count("tick totals") == 1


def test_an_unparseable_pr_url_counts_store_unknown(capsys) -> None:
    """No new class: the vocabulary is closed and mirrors diverge.go, and the
    store genuinely gave no answer for this entity.

    `compared` is NOT len(refs) in general -- two proposals pointing at one
    pull request are compared once -- so it is not an invariant an operator can
    reconcile against the sweep size."""
    totals = shadow.compare_proposal_refs([_Ref("not-a-url")], {0: shadow.LEGACY_FREE}, _parse)
    assert totals.compared == 1
    assert totals.counts[shadow.DIVERGE_STORE_UNKNOWN] == 1


def test_compare_proposal_refs_never_raises() -> None:
    """The whole contract of an observer. parse_pr_url is injected, so the
    catch cannot be narrowed to what today's caller happens to raise."""

    def _explodes(pr_url: str):
        raise RuntimeError("anything at all")

    totals = shadow.compare_proposal_refs(
        [_Ref("https://github.com/mctlhq/mctl-web/pull/42")],
        {0: shadow.LEGACY_OWNED},
        _explodes,
    )
    assert totals.counts[shadow.DIVERGE_STORE_UNKNOWN] == 1


def test_two_proposals_on_one_pr_are_compared_once(capsys) -> None:
    client = _FakeClient()
    totals = shadow.compare_proposal_refs(
        [
            _Ref("https://github.com/mctlhq/mctl-web/pull/42", slug="issue-1-x"),
            _Ref("https://github.com/mctlhq/mctl-web/pull/42", slug="issue-2-y"),
        ],
        {0: shadow.LEGACY_FREE, 1: shadow.LEGACY_OWNED},
        _parse,
        client=client,
    )
    assert totals.compared == 1
    assert [call[2] for call in client.calls] == [("mctlhq/mctl-web#42",)]
    # OWNED > FREE > UNKNOWN, explicitly, so the class does not depend on the
    # order the proposal directories happened to be listed in.
    assert "two proposals map to" in capsys.readouterr().out


# --- derived.held, through the real parser -----------------------------


@pytest.mark.parametrize(
    ("payload_derived", "want_held"),
    [
        ({"derived": {"status": "healthy", "held": True}}, True),
        ({"derived": {"status": "released", "held": False}}, False),
        # Absent block: an mctl-api that predates it. NOT False -- that would
        # say "nobody holds this", the one answer that licenses action.
        ({}, None),
        # Present but not a bool. bool("false") is True, and that coercion in
        # this field would report an unowned entity as held.
        ({"derived": {"status": "healthy", "held": "false"}}, None),
        ({"derived": None}, None),
    ],
)
def test_from_payload_parses_derived_held(payload_derived, want_held) -> None:
    """Exercised through Ownership.from_payload rather than by constructing the
    dataclass, because a key or shape regression in the PARSER would leave
    every real record at held=None and turn the whole soak into store-unknown
    -- while every test that builds Ownership(held=...) directly stayed green.
    """
    payload = {
        "entity": {"kind": "pull-request", "id": "mctlhq/mctl-web#42"},
        "phase": "review-remediation",
        "owner": {"type": "devloop-workflow", "id": "wf-1"},
        "state": "active",
        "healthy": True,
        **payload_derived,
    }
    record = Ownership.from_payload(payload)
    assert record is not None
    assert record.held is want_held


def test_a_recorded_unowned_row_uses_the_servers_held() -> None:
    """UNOWNED is not always "no record": answer_from returns it for a
    released or terminal row, which is a record and carries derived.held.

    Short-cutting to False off the verdict agrees with mctl-api today only
    because verdict_for and Derive classify those states alike -- the
    one-predicate-two-implementations coupling this module refuses everywhere
    else.
    """
    released = Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="shepherd", id="shepherd:mctl-web"),
        state="released",
        held=False,
        derived_status="released",
    )
    assert shadow.held(OwnershipAnswer(verdict=UNOWNED, ownership=released)) is False

    # The same row from a server that did not send the block: unknown, not free.
    no_block = Ownership(
        entity=EntityRef(kind="pull-request", id="mctlhq/mctl-web#42"),
        phase="review-remediation",
        owner=Owner(type="shepherd", id="shepherd:mctl-web"),
        state="released",
        held=None,
    )
    answer = OwnershipAnswer(verdict=UNOWNED, ownership=no_block)
    assert shadow.held(answer) is None
    assert shadow.classify(answer, shadow.LEGACY_OWNED).divergence_class == (
        shadow.DIVERGE_STORE_UNKNOWN
    )


def test_a_held_verdict_without_a_record_is_unknown() -> None:
    """An absence, not a free entity. Reporting False here would classify a
    driven entity as store-permits-old-forbids off a record nobody read."""
    answer = OwnershipAnswer(verdict=OWNED_BY_OTHER, ownership=None)
    assert shadow.held(answer) is None
    assert shadow.classify(answer, shadow.LEGACY_OWNED).divergence_class == (
        shadow.DIVERGE_STORE_UNKNOWN
    )


def test_a_failure_on_a_later_chunk_keeps_the_earlier_ones(monkeypatch, capsys) -> None:
    """"Keeps whatever was already compared" has to hold ACROSS chunks, not
    only within one: classifying after the whole read threw away the first
    chunk's comparisons when the second failed."""
    monkeypatch.setattr(shadow, "SHADOW_CHUNK_SIZE", 1)

    class _FailsOnTheSecond:
        def __init__(self):
            self.calls = 0

        def get_many(self, kind, phase, ids, asking=None):
            self.calls += 1
            if self.calls > 1:
                raise RuntimeError("the store blew up mid-batch")
            return {i: OwnershipAnswer(verdict=UNOWNED) for i in ids}

    totals = shadow.compare_entities(
        {"a#1": shadow.LEGACY_FREE, "a#2": shadow.LEGACY_FREE}, client=_FailsOnTheSecond()
    )
    assert totals.compared == 1
    out = capsys.readouterr().out
    assert out.count("lifecycle-shadow: divergence") == 1
    assert out.count("tick totals") == 1


def test_the_chunk_size_matches_the_clients_own() -> None:
    """The budget can only stop the compare between chunks, and get_many
    re-chunks internally — a larger value here would become several requests
    inside one un-interruptible call, and the bound would be coarser than it
    reads."""
    from orchestrator.lifecycle.client import BATCH_CHUNK_SIZE

    assert shadow.SHADOW_CHUNK_SIZE == BATCH_CHUNK_SIZE


def test_the_two_unknown_causes_are_named_apart() -> None:
    """`held()` answers None for two structurally different reasons, and the
    detail is the first thing anyone reads if this class spikes."""
    no_block = OwnershipAnswer(
        verdict=OWNED_BY_OTHER,
        ownership=Ownership(
            entity=EntityRef(kind="pull-request", id="a#1"),
            phase="review-remediation",
            owner=Owner(type="shepherd", id="shepherd:x"),
            state="active",
            held=None,
        ),
    )
    assert "predates the field" in shadow.classify(no_block, shadow.LEGACY_FREE).detail

    no_record = OwnershipAnswer(verdict=OWNED_BY_OTHER, reason="409: owned by shepherd:x")
    detail = shadow.classify(no_record, shadow.LEGACY_FREE).detail
    assert "no record attached" in detail
    # The server's own message survives: without it the line names an mctl-api
    # version skew that did not happen and drops the only explanation there is.
    assert "409: owned by shepherd:x" in detail
