"""Parity vectors for the review receipt contract (ADR 025, mctl-agents#905).

``tests/fixtures/review/review_receipt_vectors.json`` is the external contract
the Tier B Go port (mctl-api ``internal/reviewreceipts``) is tested against.
It is never written by hand: every value is produced by calling the canonical
Tier A functions in ``orchestrator/review_receipts.py``, and
``test_checked_in_fixture_is_byte_identical`` fails if the file drifts from
what Tier A builds now.

Regenerate after an intended Tier A change::

    python tests/test_review_parity_vectors.py --write

Refusal vectors carry a hard-coded ``code`` in this file; the generator also
checks that Tier A really raises it, so a vector cannot silently record an
accident.
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from orchestrator import review_receipts as rr
from orchestrator.proposal_state import PROPOSAL_DIGEST_FILES

FIXTURE = Path(__file__).parent / "fixtures" / "review" / "review_receipt_vectors.json"
FORMAT = 1

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
EXEC_AUTHOR = "we_" + "1" * 16
EXEC_REVIEW = "we_" + "2" * 16
EXEC_REVIEW_2 = "we_" + "3" * 16
EXEC_AUTHOR_UUID = "we_7ea4cf0c-d8ba-4825-9191-888e17a47eec"
EXEC_REVIEW_UUID = "we_a11b5572-fed2-482b-bedf-2b4160476cb9"
AUTHOR = "operator"
REVIEWER = "reviewer-bot"
NONCE = "nonce-0001"
CREATED = "2026-11-01T12:00:00Z"


# ---------------------------------------------------------------------------
# Builders (Tier A only)
# ---------------------------------------------------------------------------


def _spike(head: str = SHA_B, report: bytes = b"# Spike\n") -> rr.ReviewSubject:
    return rr.spike_subject(
        repository="mctlhq/mctl-agents",
        report_root="spikes/demo",
        report_path="report.md",
        source_sha=SHA_A,
        base_sha=SHA_C,
        head_sha=head,
        entries=[rr.InventoryEntry(path="report.md", content=report)],
    )


def _proposal(
    commit: str = SHA_A, tasks: bytes = b"- [ ] 1. do it\n", approval_ref: str = "", slug: str = "issue-895-demo"
) -> rr.ReviewSubject:
    files: dict[str, bytes | None] = {name: None for name in PROPOSAL_DIGEST_FILES}
    files.update({"requirements.md": b"# Requirements\n", "design.md": b"# Design\n", "tasks.md": tasks})
    return rr.proposal_subject(
        service="mctl-api",
        slug=slug,
        gitops_commit=commit,
        files=files,
        approval_ref=approval_ref,
    )


def _request(
    subject: rr.ReviewSubject,
    *,
    mode: str = "worker",
    actors: tuple[str, ...] = (AUTHOR,),
    execution: str = EXEC_AUTHOR,
    attempts: tuple[str, ...] = ("attempt-author",),
    lineage: str = "lineage-author",
    nonce: str = NONCE,
) -> rr.ReviewRequest:
    return rr.seal_request(
        subject=subject,
        mode=mode,
        author_actors=actors,
        nonce=nonce,
        authoring_execution_id=execution if mode == "worker" else "",
        authoring_attempt_ids=attempts if mode == "worker" else (),
        authoring_lineage_root=lineage if mode == "worker" else "",
        created_at=CREATED,
    )


def _all_met(subject: rr.ReviewSubject) -> list[rr.Criterion]:
    return [rr.Criterion(id=i, status="met") for i in rr.RUBRICS[rr.RUBRIC_FOR_SUBJECT[subject.kind]]]


def _receipt(
    request: rr.ReviewRequest,
    *,
    outcome: str = "PASS",
    criteria: list[rr.Criterion] | None = None,
    actor: str = REVIEWER,
    execution: str = EXEC_REVIEW,
    attempt: str = "attempt-review",
    lineage: str = "lineage-review",
) -> rr.ReviewReceipt:
    manual = request.mode == "manual"
    return rr.seal_receipt(
        request=request,
        outcome=outcome,
        criteria=criteria if criteria is not None else _all_met(request.subject),
        findings_ref=rr.ArtifactRef(name="findings", kind="review-findings", content_hash="sha256:" + "d" * 64),
        reviewer_actor=actor,
        reviewer_execution_id="" if manual else execution,
        attempt_id="" if manual else attempt,
        lineage_root="" if manual else lineage,
        created_at=CREATED,
    )


def _with_unobserved(subject: rr.ReviewSubject) -> list[rr.Criterion]:
    criteria = _all_met(subject)
    criteria[0] = rr.Criterion(id=criteria[0].id, status="unobserved")
    return criteria


# ---------------------------------------------------------------------------
# Vector sections
# ---------------------------------------------------------------------------


def _documents() -> dict[str, Any]:
    spike, proposal = _spike(), _proposal(approval_ref="aar_" + "e" * 32)
    requests = {
        "spike_worker_request": _request(spike),
        "proposal_worker_request": _request(proposal),
        "proposal_manual_request": _request(proposal, mode="manual", nonce="nonce-manual"),
        "proposal_worker_request_store_execution_id": _request(
            proposal, execution=EXEC_AUTHOR_UUID, nonce="nonce-store-uuid"
        ),
        "spike_two_authors_request": _request(spike, actors=("zed", "Alice", "alice"), attempts=("b", "a", "a")),
    }
    receipts = {
        "spike_worker_pass": _receipt(requests["spike_worker_request"]),
        "proposal_worker_pass": _receipt(requests["proposal_worker_request"]),
        "proposal_worker_changes_requested": _receipt(
            requests["proposal_worker_request"],
            outcome="CHANGES_REQUESTED",
            criteria=[rr.Criterion(id="scope_bounded", status="unmet")],
        ),
        "proposal_worker_unobservable": _receipt(
            requests["proposal_worker_request"],
            outcome="UNOBSERVABLE",
            criteria=_with_unobserved(requests["proposal_worker_request"].subject),
        ),
        "proposal_manual_pass": _receipt(requests["proposal_manual_request"]),
        "proposal_worker_pass_store_execution_id": _receipt(
            requests["proposal_worker_request_store_execution_id"], execution=EXEC_REVIEW_UUID
        ),
    }
    return {
        "requests": [
            {"name": n, "document": r.to_dict(), "expected": {"content_hash": r.content_hash, "id": r.request_id}}
            for n, r in requests.items()
        ],
        "receipts": [
            {"name": n, "document": r.to_dict(), "expected": {"content_hash": r.content_hash, "id": r.receipt_id}}
            for n, r in receipts.items()
        ],
    }


def _refusals() -> list[dict[str, Any]]:
    """(name, kind, mutator over a sealed document, expected code). The mutated
    document keeps the original ``content_hash``/id unless the mutator changes
    them, which is what makes identity checks bite."""
    spike_req = _request(_spike())
    prop_req = _request(_proposal())
    prop_rc = _receipt(prop_req)
    spike_rc = _receipt(spike_req)

    def alter(doc: dict[str, Any], **changes: Any) -> dict[str, Any]:
        out = copy.deepcopy(doc)
        out.update(changes)
        return out

    def drop(doc: dict[str, Any], key: str) -> dict[str, Any]:
        out = copy.deepcopy(doc)
        del out[key]
        return out

    rd, pd, sd = prop_req.to_dict(), prop_rc.to_dict(), spike_rc.to_dict()
    long_subject = copy.deepcopy(rd["subject"])
    long_subject["slug"] = "a" * 257
    spike_with_proposal_field = copy.deepcopy(spike_req.to_dict()["subject"])
    spike_with_proposal_field["service"] = "mctl-api"
    unmet = [{"id": "scope_bounded", "status": "unmet"}]

    specs: list[tuple[str, str, dict[str, Any], str]] = [
        ("request_unknown_key", "request", alter(rd, extra="x"), rr.CODE_UNKNOWN_KEY),
        ("request_wrong_api_version", "request", alter(rd, api_version="review.mctl.ai/v1"), rr.CODE_MALFORMED),
        ("request_wrong_kind", "request", alter(rd, kind="ReviewReceipt"), rr.CODE_MALFORMED),
        ("request_mode_outside_choices", "request", alter(rd, mode="auto"), rr.CODE_MALFORMED),
        ("request_nonce_not_a_string", "request", alter(rd, nonce=7), rr.CODE_MALFORMED),
        ("request_nonce_missing", "request", drop(rd, "nonce"), rr.CODE_MALFORMED),
        ("request_overlong_slug", "request", alter(rd, subject=long_subject), rr.CODE_MALFORMED),
        ("request_overlong_actor", "request", alter(rd, author_actors=["a" * 257]), rr.CODE_MALFORMED),
        (
            "request_too_many_actors",
            "request",
            alter(rd, author_actors=[f"a{i}" for i in range(65)]),
            rr.CODE_MALFORMED,
        ),
        ("request_bad_authoring_execution_id", "request", alter(rd, authoring_execution_id="nope"), rr.CODE_MALFORMED),
        (
            "request_uppercase_store_execution_id",
            "request",
            alter(rd, authoring_execution_id=EXEC_AUTHOR_UUID.upper().replace("WE_", "we_")),
            rr.CODE_MALFORMED,
        ),
        (
            "request_store_execution_id_misplaced_hyphen",
            "request",
            alter(rd, authoring_execution_id="we_7ea4cf0cd-8ba-4825-9191-888e17a47eec"),
            rr.CODE_MALFORMED,
        ),
        (
            "request_braced_store_execution_id",
            "request",
            alter(rd, authoring_execution_id="we_{" + EXEC_AUTHOR_UUID[3:] + "}"),
            rr.CODE_MALFORMED,
        ),
        (
            "request_store_execution_id_trailing_newline",
            "request",
            alter(rd, authoring_execution_id=EXEC_AUTHOR_UUID + "\n"),
            rr.CODE_MALFORMED,
        ),
        ("request_bad_created_at", "request", alter(rd, created_at="yesterday"), rr.CODE_MALFORMED),
        ("request_rubric_mismatch", "request", alter(rd, rubric="spike_v1"), rr.CODE_RUBRIC_MISMATCH),
        ("request_altered_nonce_keeps_hash", "request", alter(rd, nonce="nonce-forged"), rr.CODE_BAD_IDENTITY),
        (
            "request_altered_actors_keep_hash",
            "request",
            alter(rd, author_actors=["somebody-else"]),
            rr.CODE_BAD_IDENTITY,
        ),
        ("request_wrong_id_prefix_hash", "request", alter(rd, request_id="rr_" + "0" * 16), rr.CODE_BAD_IDENTITY),
        ("request_wrong_content_hash", "request", alter(rd, content_hash="sha256:" + "0" * 64), rr.CODE_BAD_IDENTITY),
        (
            "request_spike_subject_with_proposal_field",
            "request",
            alter(spike_req.to_dict(), subject=spike_with_proposal_field),
            rr.CODE_BAD_SUBJECT,
        ),
        ("receipt_unknown_key", "receipt", alter(pd, extra="x"), rr.CODE_UNKNOWN_KEY),
        ("receipt_wrong_kind", "receipt", alter(pd, kind="ReviewRequest"), rr.CODE_MALFORMED),
        ("receipt_criteria_not_a_list", "receipt", alter(pd, criteria="all"), rr.CODE_MALFORMED),
        (
            "receipt_unknown_criterion",
            "receipt",
            alter(pd, criteria=[{"id": "vibes", "status": "met"}]),
            rr.CODE_MALFORMED,
        ),
        (
            "receipt_duplicate_criterion",
            "receipt",
            alter(pd, criteria=unmet + unmet, outcome="CHANGES_REQUESTED"),
            rr.CODE_MALFORMED,
        ),
        (
            "receipt_bad_criterion_status",
            "receipt",
            alter(pd, criteria=[{"id": "scope_bounded", "status": "ok"}]),
            rr.CODE_MALFORMED,
        ),
        ("receipt_outcome_outside_choices", "receipt", alter(pd, outcome="MAYBE"), rr.CODE_MALFORMED),
        ("receipt_assurance_must_match_mode", "receipt", alter(pd, assurance="advisory"), rr.CODE_MALFORMED),
        (
            "receipt_manual_claims_independent",
            "receipt",
            alter(_receipt(_request(_proposal(), mode="manual")).to_dict(), assurance="independent"),
            rr.CODE_MALFORMED,
        ),
        ("receipt_rubric_mismatch", "receipt", alter(pd, rubric="spike_v1"), rr.CODE_RUBRIC_MISMATCH),
        ("receipt_bad_request_id_shape", "receipt", alter(pd, request_id="rr_short"), rr.CODE_MALFORMED),
        ("receipt_bad_evidence_ref", "receipt", alter(pd, evidence_ref="evidence-1"), rr.CODE_MALFORMED),
        (
            "receipt_pass_with_unmet_criterion",
            "receipt",
            alter(
                pd,
                criteria=[
                    {"id": i, "status": "unmet" if i == "scope_bounded" else "met"}
                    for i in rr.RUBRICS[rr.RUBRIC_PROPOSAL]
                ],
            ),
            rr.CODE_PASS_WITH_UNOBSERVED,
        ),
        (
            "receipt_pass_with_unobserved_criterion",
            "receipt",
            alter(
                pd,
                criteria=[
                    {"id": i, "status": "unobserved" if i == "scope_bounded" else "met"}
                    for i in rr.RUBRICS[rr.RUBRIC_PROPOSAL]
                ],
            ),
            rr.CODE_PASS_WITH_UNOBSERVED,
        ),
        (
            "receipt_pass_with_missing_criterion",
            "receipt",
            alter(pd, criteria=[{"id": "scope_bounded", "status": "met"}]),
            rr.CODE_PASS_WITH_UNOBSERVED,
        ),
        (
            "receipt_changes_requested_with_unobserved",
            "receipt",
            alter(pd, outcome="CHANGES_REQUESTED", criteria=[{"id": "scope_bounded", "status": "unobserved"}]),
            rr.CODE_UNOBSERVED_OUTCOME,
        ),
        ("receipt_altered_outcome_keeps_hash", "receipt", alter(sd, outcome="NEEDS_HUMAN"), rr.CODE_BAD_IDENTITY),
        ("receipt_wrong_id", "receipt", alter(pd, receipt_id="rc_" + "0" * 16), rr.CODE_BAD_IDENTITY),
    ]
    out = []
    mismatches: list[str] = []
    for name, kind, doc, code in specs:
        parser = rr.ReviewRequest if kind == "request" else rr.ReviewReceipt
        with pytest.raises(rr.ReviewError) as info:
            parser.from_dict(doc)
        mismatches.append(f"{name}: raised {info.value.code}, expects {code}") if info.value.code != code else None
        out.append({"name": name, "kind": kind, "document": doc, "code": code})
    for name, subject, code in _subject_refusals():
        with pytest.raises(rr.ReviewError) as info:
            rr.ReviewSubject.from_dict(subject)
        mismatches.append(f"{name}: raised {info.value.code}, expects {code}") if info.value.code != code else None
        out.append({"name": name, "kind": "subject", "document": subject, "code": code})
    assert not mismatches, "Tier A disagrees with the stated intent:\n" + "\n".join(mismatches)
    return out


def _subject_refusals() -> list[tuple[str, dict[str, Any], str]]:
    spike, proposal = _spike().to_dict(), _proposal().to_dict()

    def alter(doc: dict[str, Any], **changes: Any) -> dict[str, Any]:
        return {**doc, **changes}

    return [
        ("subject_unknown_key", alter(proposal, extra="x"), rr.CODE_UNKNOWN_KEY),
        ("subject_unknown_kind", alter(proposal, kind="pr"), rr.CODE_MALFORMED),
        ("subject_proposal_with_spike_field", alter(proposal, head_sha=SHA_A), rr.CODE_BAD_SUBJECT),
        ("subject_spike_bad_repository", alter(spike, repository="not a repo"), rr.CODE_BAD_SUBJECT),
        ("subject_spike_traversal_path", alter(spike, report_root="../x"), rr.CODE_BAD_SUBJECT),
        ("subject_spike_non_md_report", alter(spike, report_path="report.txt"), rr.CODE_BAD_SUBJECT),
        ("subject_spike_short_sha", alter(spike, head_sha="abc123"), rr.CODE_BAD_SUBJECT),
        ("subject_spike_uppercase_sha", alter(spike, head_sha="A" * 40), rr.CODE_BAD_SUBJECT),
        ("subject_spike_bad_digest", alter(spike, inventory_digest="sha256:xyz"), rr.CODE_BAD_SUBJECT),
        ("subject_spike_wrong_digest_version", alter(spike, digest_version="v0"), rr.CODE_BAD_SUBJECT),
        ("subject_proposal_bad_slug", alter(proposal, slug="Bad Slug"), rr.CODE_BAD_SUBJECT),
        ("subject_proposal_bad_commit", alter(proposal, gitops_commit="main"), rr.CODE_BAD_SUBJECT),
        ("subject_proposal_bad_digest", alter(proposal, content_digest="abc"), rr.CODE_BAD_SUBJECT),
        ("subject_proposal_bad_approval_ref", alter(proposal, approval_ref="grant-1"), rr.CODE_MALFORMED),
        ("subject_missing_field", {k: v for k, v in proposal.items() if k != "slug"}, rr.CODE_MALFORMED),
    ]


def _acceptances() -> list[dict[str, Any]]:
    """Documents Tier A accepts but normalises; Tier B must do the same."""
    req = _request(_spike(), actors=("zed", "alice"), attempts=("b", "a"))
    doc = req.to_dict()
    doc["author_actors"] = ["zed", "alice", "alice"]  # unsorted and duplicated
    doc["authoring_attempt_ids"] = ["b", "a"]
    parsed = rr.ReviewRequest.from_dict(doc)
    unsealed = copy.deepcopy(req.to_dict())
    unsealed["content_hash"] = ""
    unsealed["request_id"] = ""
    unsealed_parsed = rr.ReviewRequest.from_dict(unsealed)
    return [
        {
            "name": "request_actor_lists_are_sorted_and_deduplicated_on_parse",
            "kind": "request",
            "document": doc,
            "normalised": parsed.to_dict(),
        },
        {
            "name": "request_without_identity_parses_unsealed",
            "kind": "request",
            "document": unsealed,
            "normalised": unsealed_parsed.to_dict(),
        },
    ]


def _gate() -> list[dict[str, Any]]:
    prop = _proposal()
    prop_req = _request(prop)
    base = _receipt(prop_req)
    cases: list[dict[str, Any]] = []

    def add(name, expected, *, live=prop, request=prop_req, receipts=(base,), required="independent", note=""):
        got: str
        try:
            got = rr.evaluate_review_gate(
                live=live, request=request, receipts=list(receipts), required_assurance=required
            )
        except ValueError:
            got = "error"
        assert got == expected, f"{name}: Tier A answered {got}, vector expects {expected}"
        cases.append(
            {
                "name": name,
                "live": live.to_dict() if live else None,
                "request": request.to_dict() if request else None,
                "receipts": [r.to_dict() for r in receipts],
                "required_assurance": required,
                "expected": expected,
                "note": note,
            }
        )

    # Outcomes
    add("satisfied", rr.GATE_SATISFIED)
    add(
        "changes_requested",
        rr.GATE_CHANGES_REQUESTED,
        receipts=[_receipt(prop_req, outcome="CHANGES_REQUESTED", criteria=[rr.Criterion("scope_bounded", "unmet")])],
    )
    add(
        "needs_human",
        rr.GATE_NEEDS_HUMAN,
        receipts=[_receipt(prop_req, outcome="NEEDS_HUMAN", criteria=_with_unobserved(prop))],
    )
    add(
        "unobservable",
        rr.GATE_UNOBSERVABLE,
        receipts=[_receipt(prop_req, outcome="UNOBSERVABLE", criteria=_with_unobserved(prop))],
    )
    add(
        "spike_satisfied",
        rr.GATE_SATISFIED,
        live=_spike(),
        request=_request(_spike()),
        receipts=[_receipt(_request(_spike()))],
    )
    # Binding
    add("live_unobserved_is_unknown", rr.GATE_UNKNOWN, live=None)
    add("empty_pool_is_missing", rr.GATE_MISSING, receipts=[])
    add(
        "only_other_identity_is_missing",
        rr.GATE_MISSING,
        live=_proposal(),
        receipts=[_receipt(_request(_proposal(slug="other")))],
    )
    changed = _proposal(tasks=b"- [ ] 1. changed\n")
    changed_req = _request(changed)
    add("changed_bytes_are_stale", rr.GATE_STALE, live=changed, request=changed_req)
    spike_new = _spike(report=b"# Spike v2\n")
    add(
        "changed_spike_digest_is_stale",
        rr.GATE_STALE,
        live=spike_new,
        request=_request(spike_new),
        receipts=[_receipt(_request(_spike()))],
    )
    spike_head = _spike(head=SHA_C)
    add(
        "moved_spike_head_is_stale",
        rr.GATE_STALE,
        live=spike_head,
        request=_request(spike_head),
        receipts=[_receipt(_request(_spike()))],
    )
    later = _proposal(commit=SHA_B, approval_ref="aar_" + "f" * 32)
    add("proposal_gitops_commit_and_approval_are_provenance", rr.GATE_SATISFIED, live=later)
    # Request (mctl-agents#971 rule 1 and the unknown family)
    add("no_request_is_unknown", rr.GATE_UNKNOWN, request=None)
    add(
        "request_bound_to_older_revision_is_unknown",
        rr.GATE_UNKNOWN,
        request=_request(_proposal(tasks=b"- [ ] 1. old\n")),
    )
    spike_old = _spike(head=SHA_C)
    add(
        "spike_request_bound_to_other_head_is_unknown",
        rr.GATE_UNKNOWN,
        live=_spike(),
        request=_request(spike_old),
        receipts=[_receipt(_request(_spike()))],
    )
    no_exec = rr.seal_request(subject=prop, mode="worker", author_actors=[AUTHOR], nonce="n-noexec", created_at=CREATED)
    add(
        "worker_request_without_authoring_execution_is_unknown",
        rr.GATE_UNKNOWN,
        request=no_exec,
        receipts=[_receipt(no_exec)],
    )
    unsealed = rr.ReviewRequest(
        subject=prop, rubric=rr.RUBRIC_PROPOSAL, mode="worker", author_actors=(AUTHOR,), nonce="n-unsealed",
        authoring_execution_id=EXEC_AUTHOR,
    )  # fmt: skip
    add("unsealed_request_is_unknown", rr.GATE_UNKNOWN, request=unsealed)
    # Pool scoping (rule 2)
    other_req = _request(prop, nonce="nonce-other")
    add("receipt_of_another_request_id_is_missing", rr.GATE_MISSING, receipts=[_receipt(other_req)])
    add(
        "other_request_receipt_does_not_mask_own",
        rr.GATE_CHANGES_REQUESTED,
        receipts=[
            _receipt(prop_req, outcome="CHANGES_REQUESTED", criteria=[rr.Criterion("scope_bounded", "unmet")]),
            _receipt(other_req),
        ],
    )
    add(
        "latest_receipt_wins",
        rr.GATE_SATISFIED,
        receipts=[
            _receipt(prop_req, outcome="CHANGES_REQUESTED", criteria=[rr.Criterion("scope_bounded", "unmet")]),
            _receipt(prop_req, execution=EXEC_REVIEW_2, attempt="attempt-review-2"),
        ],
    )
    add(
        "earlier_pass_does_not_beat_latest_changes",
        rr.GATE_CHANGES_REQUESTED,
        receipts=[
            _receipt(prop_req),
            _receipt(
                prop_req,
                outcome="CHANGES_REQUESTED",
                criteria=[rr.Criterion("scope_bounded", "unmet")],
                execution=EXEC_REVIEW_2,
                attempt="attempt-review-2",
            ),
        ],
    )
    # Independence (rules 3 and 4)
    store_req = _request(prop, execution=EXEC_AUTHOR_UUID, nonce="nonce-store-uuid")
    add(
        "store_execution_ids_are_independent",
        rr.GATE_SATISFIED,
        request=store_req,
        receipts=[_receipt(store_req, execution=EXEC_REVIEW_UUID)],
    )
    add(
        "same_store_execution_is_not_independent",
        rr.GATE_NOT_INDEPENDENT,
        request=store_req,
        receipts=[_receipt(store_req, execution=EXEC_AUTHOR_UUID)],
    )
    add("same_actor_is_not_independent", rr.GATE_NOT_INDEPENDENT, receipts=[_receipt(prop_req, actor=AUTHOR)])
    add("actor_compare_is_case_insensitive", rr.GATE_NOT_INDEPENDENT, receipts=[_receipt(prop_req, actor="Operator")])
    add("empty_reviewer_actor_is_not_independent", rr.GATE_NOT_INDEPENDENT, receipts=[_receipt(prop_req, actor="")])
    add(
        "same_execution_is_not_independent",
        rr.GATE_NOT_INDEPENDENT,
        receipts=[_receipt(prop_req, execution=EXEC_AUTHOR)],
    )
    add(
        "authoring_attempt_is_not_independent",
        rr.GATE_NOT_INDEPENDENT,
        receipts=[_receipt(prop_req, attempt="attempt-author")],
    )
    add(
        "same_lineage_is_not_independent",
        rr.GATE_NOT_INDEPENDENT,
        receipts=[_receipt(prop_req, lineage="lineage-author")],
    )
    add(
        "worker_receipt_without_execution_is_not_independent",
        rr.GATE_NOT_INDEPENDENT,
        receipts=[_receipt(prop_req, execution="")],
    )
    add(
        "worker_receipt_without_attempt_is_not_independent",
        rr.GATE_NOT_INDEPENDENT,
        receipts=[_receipt(prop_req, attempt="")],
    )
    add(
        "worker_receipt_without_lineage_is_not_independent",
        rr.GATE_NOT_INDEPENDENT,
        receipts=[_receipt(prop_req, lineage="")],
    )
    add(
        "independent_receipt_survives_a_dependent_one",
        rr.GATE_SATISFIED,
        receipts=[_receipt(prop_req), _receipt(prop_req, actor=AUTHOR, execution=EXEC_REVIEW_2, attempt="a2")],
    )
    mixed = _request(prop, actors=("Alice", "bob"), attempts=("a1", "a2"))
    add(
        "any_author_actor_blocks_case_insensitively",
        rr.GATE_NOT_INDEPENDENT,
        request=mixed,
        receipts=[_receipt(mixed, actor="BOB")],
    )
    add(
        "mixed_case_author_actor_blocks_a_lowercase_reviewer",
        rr.GATE_NOT_INDEPENDENT,
        request=mixed,
        receipts=[_receipt(mixed, actor="alice")],
    )
    # Assurance
    manual_req = _request(prop, mode="manual", nonce="nonce-manual")
    manual_rc = _receipt(manual_req, actor="human-admin")
    add(
        "manual_receipt_is_advisory_only_when_independent_required",
        rr.GATE_ADVISORY_ONLY,
        request=manual_req,
        receipts=[manual_rc],
    )
    add("manual_receipt_satisfies_any", rr.GATE_SATISFIED, request=manual_req, receipts=[manual_rc], required="any")
    add(
        "manual_receipt_is_still_checked_for_actor",
        rr.GATE_NOT_INDEPENDENT,
        request=manual_req,
        receipts=[_receipt(manual_req, actor=AUTHOR)],
        required="any",
    )
    add(
        "advisory_only_is_not_satisfied_by_worker_request_pool_of_manual",
        rr.GATE_ADVISORY_ONLY,
        request=manual_req,
        receipts=[manual_rc, manual_rc],
    )
    add("unknown_required_assurance_is_an_error", "error", required="whatever")
    return cases


def build_vectors() -> dict[str, Any]:
    return {
        "format": FORMAT,
        "source": "orchestrator/review_receipts.py (mctl-agents), built by tests/test_review_parity_vectors.py",
        "note": "Generated; do not edit by hand. Regenerate: python tests/test_review_parity_vectors.py --write",
        "documents": _documents(),
        "parse_refusals": _refusals(),
        "parse_acceptances": _acceptances(),
        "gate": _gate(),
    }


def render() -> str:
    return json.dumps(build_vectors(), indent=2, sort_keys=True, ensure_ascii=False) + "\n"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_checked_in_fixture_is_byte_identical():
    assert FIXTURE.read_text() == render(), (
        "fixture drifted from Tier A; run: python tests/test_review_parity_vectors.py --write"
    )


def test_every_gate_outcome_and_the_four_971_rules_are_covered():
    vectors = json.loads(FIXTURE.read_text())
    expected = {v["expected"] for v in vectors["gate"]}
    assert expected >= {
        rr.GATE_SATISFIED, rr.GATE_STALE, rr.GATE_MISSING, rr.GATE_UNKNOWN, rr.GATE_ADVISORY_ONLY,
        rr.GATE_NOT_INDEPENDENT, rr.GATE_CHANGES_REQUESTED, rr.GATE_NEEDS_HUMAN, rr.GATE_UNOBSERVABLE, "error",
    }  # fmt: skip
    names = {v["name"] for v in vectors["gate"]}
    assert {
        "request_bound_to_older_revision_is_unknown",
        "receipt_of_another_request_id_is_missing",
        "worker_receipt_without_execution_is_not_independent",
        "worker_receipt_without_attempt_is_not_independent",
        "worker_receipt_without_lineage_is_not_independent",
        "actor_compare_is_case_insensitive",
        "store_execution_ids_are_independent",
        "same_store_execution_is_not_independent",
        "mixed_case_author_actor_blocks_a_lowercase_reviewer",
    } <= names


def test_sealed_documents_round_trip_with_their_expected_identity():
    vectors = json.loads(FIXTURE.read_text())["documents"]
    for item in vectors["requests"]:
        parsed = rr.ReviewRequest.from_dict(item["document"])
        assert (parsed.content_hash, parsed.request_id) == (item["expected"]["content_hash"], item["expected"]["id"])
        assert parsed.to_dict() == item["document"]
    for item in vectors["receipts"]:
        parsed = rr.ReviewReceipt.from_dict(item["document"])
        assert (parsed.content_hash, parsed.receipt_id) == (item["expected"]["content_hash"], item["expected"]["id"])
        assert parsed.to_dict() == item["document"]


def test_gate_vectors_replay_from_parsed_documents():
    for v in json.loads(FIXTURE.read_text())["gate"]:
        live = rr.ReviewSubject.from_dict(v["live"]) if v["live"] else None
        request = rr.ReviewRequest.from_dict(v["request"]) if v["request"] and v["request"]["content_hash"] else None
        if v["request"] and not v["request"]["content_hash"]:
            # unsealed request: parse() accepts it, the gate must still answer unknown
            request = rr.ReviewRequest.from_dict(v["request"])
        receipts = [rr.ReviewReceipt.from_dict(r) for r in v["receipts"]]
        try:
            got = rr.evaluate_review_gate(
                live=live, request=request, receipts=receipts, required_assurance=v["required_assurance"]
            )
        except ValueError:
            got = "error"
        assert got == v["expected"], v["name"]


if __name__ == "__main__":
    if "--write" in sys.argv:
        FIXTURE.parent.mkdir(parents=True, exist_ok=True)
        FIXTURE.write_text(render())
        print(f"wrote {FIXTURE}")
    else:
        print(__doc__)
