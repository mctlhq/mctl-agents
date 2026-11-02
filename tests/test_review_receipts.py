"""Independent review receipts (ADR 025, mctl-agents#905)."""
# ruff: noqa: S106

from __future__ import annotations

import ast
import asyncio
import base64
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from orchestrator import review_receipts as rr
from orchestrator.proposal_state import PROPOSAL_DIGEST_FILES, content_digest_of

FIXTURE = Path(__file__).parent / "fixtures" / "review" / "spike_digest_vectors.json"
SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40
EXEC_AUTHOR = "we_" + "1" * 16
EXEC_REVIEW = "we_" + "2" * 16


def _entries(raw):
    return [rr.InventoryEntry(path=e["path"], content=base64.b64decode(e["content_b64"])) for e in raw]


def _vectors():
    return json.loads(FIXTURE.read_text())["vectors"]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda v: v["name"])
def test_golden_vectors(vector):
    assert rr.spike_report_digest(_entries(vector["entries"]), report_path=vector["report_path"]) == vector["digest"]


def test_entry_order_does_not_matter_and_moving_bytes_changes_digest():
    by_name = {v["name"]: v for v in _vectors()}
    nested = _entries(by_name["nested_measurements"]["entries"])
    assert (
        rr.spike_report_digest(list(reversed(nested)), report_path="report.md")
        == by_name["nested_measurements"]["digest"]
    )
    assert by_name["move_a"]["digest"] != by_name["move_b"]["digest"]


def _digest(entries, report_path="report.md"):
    return rr.spike_report_digest(entries, report_path=report_path)


REPORT = rr.InventoryEntry("report.md", b"r")


@pytest.mark.parametrize(
    "extra",
    [
        rr.InventoryEntry("measurements/l", b"x", mode="120000"),
        rr.InventoryEntry("measurements/s", b"x", mode="160000"),
        rr.InventoryEntry("measurements/../x", b"x"),
        rr.InventoryEntry("measurements/./x", b"x"),
        rr.InventoryEntry("measurements\\x", b"x"),
        rr.InventoryEntry("measurements/a\x00b", b"x"),
        rr.InventoryEntry("/measurements/x", b"x"),
        rr.InventoryEntry("measurements/\udcff", b"x"),
        rr.InventoryEntry("other/x", b"x"),
        rr.InventoryEntry("measurements/A", b"x"),
    ],
)
def test_digest_refusals(extra):
    entries = [REPORT, extra]
    if extra.path == "measurements/A":
        entries.append(rr.InventoryEntry("measurements/a", b"y"))
    with pytest.raises(rr.SpikeDigestError):
        _digest(entries)


def test_digest_refuses_missing_report_and_oversize(monkeypatch):
    with pytest.raises(rr.SpikeDigestError):
        _digest([rr.InventoryEntry("measurements/x", b"x")])
    monkeypatch.setattr(rr, "MAX_SPIKE_BYTES", 3)
    with pytest.raises(rr.SpikeDigestError):
        _digest([REPORT, rr.InventoryEntry("measurements/x", b"xxxx")])
    monkeypatch.setattr(rr, "MAX_SPIKE_FILES", 1)
    with pytest.raises(rr.SpikeDigestError):
        _digest([REPORT, rr.InventoryEntry("measurements/x", b"")])


# -- reader -----------------------------------------------------------------


def _blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\x00" % len(data) + data).hexdigest()  # noqa: S324 - git object id


def _github(files: dict[str, bytes], *, truncated=False, fail_blob=False, mode="100644"):
    """A fake GitHub: repo root holds `spikes/s1/` with the given files."""
    blobs = {_blob_sha(c): c for c in files.values()}
    leaf = [{"path": p, "type": "blob", "mode": mode, "sha": _blob_sha(c), "size": len(c)} for p, c in files.items()]

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/trees/" + SHA_A):
            return httpx.Response(200, json={"tree": [{"path": "spikes", "type": "tree", "sha": "t1"}]})
        if path.endswith("/trees/t1"):
            return httpx.Response(200, json={"tree": [{"path": "s1", "type": "tree", "sha": "t2"}]})
        if path.endswith("/trees/t2"):
            return httpx.Response(200, json={"tree": leaf, "truncated": truncated})
        if "/blobs/" in path:
            if fail_blob:
                return httpx.Response(500)
            data = blobs[path.rsplit("/", 1)[1]]
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(data).decode()})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _read(client):
    async def run():
        async with client:
            return await rr.read_spike_inventory("o/r", "spikes/s1", "report.md", SHA_A, token="t", client=client)

    return asyncio.run(run())


FILES = {"report.md": b"# r\n", "measurements/a.csv": b"1\n", "notes.txt": b"ignored"}


def test_reader_returns_complete_inventory_and_ignores_other_files():
    entries = _read(_github(FILES))
    assert sorted(e.path for e in entries) == ["measurements/a.csv", "report.md"]
    assert _digest(entries) == _digest(
        [rr.InventoryEntry("report.md", b"# r\n"), rr.InventoryEntry("measurements/a.csv", b"1\n")]
    )


@pytest.mark.parametrize(
    "kwargs", [{"truncated": True}, {"fail_blob": True}, {"mode": "120000"}, {"mode": "160000"}], ids=str
)
def test_reader_refuses(kwargs):
    with pytest.raises(rr.SpikeDigestError):
        _read(_github(FILES, **kwargs))


def test_reader_refuses_blob_bytes_not_matching_sha():
    client = _github(FILES)
    original = client._transport.handle_async_request  # type: ignore[attr-defined]

    async def tampered(request):
        response = await original(request)
        if "/blobs/" in request.url.path:
            return httpx.Response(200, json={"encoding": "base64", "content": base64.b64encode(b"zz").decode()})
        return response

    client._transport.handle_async_request = tampered  # type: ignore[attr-defined]
    with pytest.raises(rr.SpikeDigestError):
        _read(client)


def test_reader_refuses_short_sha():
    with pytest.raises(rr.SpikeDigestError):
        asyncio.run(
            rr.read_spike_inventory("o/r", "spikes/s1", "report.md", "abc", token="t", client=httpx.AsyncClient())
        )


# -- documents --------------------------------------------------------------

BASE_ENTRIES = [rr.InventoryEntry("report.md", b"r"), rr.InventoryEntry("measurements/m", b"1")]


def spike(entries=BASE_ENTRIES, *, source=SHA_A, base=SHA_B, head=SHA_C):
    return rr.spike_subject(
        repository="mctlhq/.github", report_root="roadmap/spikes/s1", report_path="report.md",
        source_sha=source, base_sha=base, head_sha=head, entries=entries,
    )  # fmt: skip


def request(
    subject=None, *, mode="worker", authors=("operator",), execution=EXEC_AUTHOR, attempts=("at-1",), lineage="trace-1"
):
    return rr.seal_request(
        subject=subject or spike(), mode=mode, author_actors=authors, nonce="n1",
        authoring_execution_id=execution, authoring_attempt_ids=attempts, authoring_lineage_root=lineage,
    )  # fmt: skip


def criteria(rubric=rr.RUBRIC_SPIKE, status="met"):
    return [rr.Criterion(c, status) for c in rr.RUBRICS[rubric]]


def receipt(
    req,
    outcome="PASS",
    *,
    actor="service:reviewer",
    execution=EXEC_REVIEW,
    attempt="at-9",
    lineage="trace-9",
    crit=None,
):
    return rr.seal_receipt(
        request=req, outcome=outcome, criteria=crit if crit is not None else criteria(req.rubric),
        reviewer_actor=actor, reviewer_execution_id=execution, attempt_id=attempt, lineage_root=lineage,
    )  # fmt: skip


def gate(live, req, receipts, assurance="independent"):
    return rr.evaluate_review_gate(live=live, request=req, receipts=receipts, required_assurance=assurance)


def test_seal_roundtrip_and_ids():
    req = request()
    rc = receipt(req)
    assert req.request_id.startswith("rr_") and len(req.request_id) == 19
    assert rc.receipt_id.startswith("rc_")
    assert rr.ReviewRequest.from_dict(json.loads(json.dumps(req.to_dict()))) == req
    assert rr.ReviewReceipt.from_dict(json.loads(json.dumps(rc.to_dict()))) == rc


def test_from_dict_rejects_unknown_keys_and_tampering():
    req = request()
    with pytest.raises(rr.ReviewError) as exc:
        rr.ReviewRequest.from_dict({**req.to_dict(), "extra": 1})
    assert exc.value.code == rr.CODE_UNKNOWN_KEY
    with pytest.raises(rr.ReviewError) as exc:
        rr.ReviewRequest.from_dict({**req.to_dict(), "nonce": "other"})
    assert exc.value.code == rr.CODE_BAD_IDENTITY
    bad_subject = {**req.subject.to_dict(), "source_sha": "abc123"}
    with pytest.raises(rr.ReviewError):
        rr.ReviewSubject.from_dict(bad_subject)


def test_pass_with_unobserved_rejected_and_unobservable_accepted():
    req = request()
    crit = criteria()
    crit[0] = rr.Criterion(crit[0].id, "unobserved")
    with pytest.raises(rr.ReviewError) as exc:
        receipt(req, "PASS", crit=crit)
    assert exc.value.code == rr.CODE_PASS_WITH_UNOBSERVED
    with pytest.raises(rr.ReviewError) as exc:
        receipt(req, "PASS", crit=criteria()[:-1])
    assert exc.value.code == rr.CODE_PASS_WITH_UNOBSERVED
    assert receipt(req, "UNOBSERVABLE", crit=crit).outcome == "UNOBSERVABLE"
    assert receipt(req, "NEEDS_HUMAN", crit=crit).outcome == "NEEDS_HUMAN"
    with pytest.raises(rr.ReviewError):
        receipt(req, "CHANGES_REQUESTED", crit=crit)
    with pytest.raises(rr.ReviewError):
        receipt(req, "APPROVED")


def test_rubric_must_match_subject_kind():
    req = request()
    with pytest.raises(rr.ReviewError) as exc:
        rr.seal_receipt(request=req, outcome="UNOBSERVABLE", criteria=[rr.Criterion("scope_bounded", "unobserved")])
    assert exc.value.code == rr.CODE_MALFORMED
    data = {**req.to_dict(), "rubric": rr.RUBRIC_PROPOSAL}
    with pytest.raises(rr.ReviewError) as exc:
        rr.ReviewRequest.from_dict(data)
    assert exc.value.code == rr.CODE_RUBRIC_MISMATCH


def test_manual_mode_forces_advisory():
    rc = receipt(request(mode="manual"))
    assert rc.assurance == "advisory"
    forged = {**rc.to_dict(), "assurance": "independent"}
    with pytest.raises(rr.ReviewError):
        rr.ReviewReceipt.from_dict(forged)


# -- proposal subject (T3) --------------------------------------------------


def proposal(files):
    return rr.proposal_subject(service="mctl-agents", slug="issue-1", gitops_commit=SHA_A, files=files)


def test_proposal_subject_uses_content_digest_of():
    files = {name: f"{name} body".encode() for name in PROPOSAL_DIGEST_FILES}
    files["proposed-content.md"] = None
    assert proposal(files).content_digest == content_digest_of(files)
    assert "digest" not in {n for n in dir(rr) if n.startswith("proposal_") and "digest" in n}


# -- gate -------------------------------------------------------------------


def test_gate_satisfied_and_stale_on_every_binding_change():
    req = request()
    rc = receipt(req)
    assert gate(req.subject, req, [rc]) == rr.GATE_SATISFIED
    changed = {
        "report byte": [rr.InventoryEntry("report.md", b"R"), BASE_ENTRIES[1]],
        "measurement byte": [BASE_ENTRIES[0], rr.InventoryEntry("measurements/m", b"2")],
        "added measurement": [*BASE_ENTRIES, rr.InventoryEntry("measurements/n", b"")],
    }
    for name, entries in changed.items():
        assert gate(spike(entries), req, [rc]) == rr.GATE_STALE, name
    assert gate(spike(source=SHA_B), req, [rc]) == rr.GATE_STALE
    assert gate(spike(base=SHA_C), req, [rc]) == rr.GATE_STALE
    assert gate(spike(head=SHA_A), req, [rc]) == rr.GATE_STALE


def test_gate_missing_for_other_subject_and_empty():
    req = request()
    other = rr.spike_subject(
        repository="mctlhq/.github", report_root="roadmap/spikes/other", report_path="report.md",
        source_sha=SHA_A, base_sha=SHA_B, head_sha=SHA_C, entries=BASE_ENTRIES,
    )  # fmt: skip
    assert gate(req.subject, req, []) == rr.GATE_MISSING
    assert gate(other, req, [receipt(req)]) == rr.GATE_MISSING


def test_gate_proposal_amendment_stale_and_later_commit_same_bytes_satisfied():
    files = {name: b"x" for name in PROPOSAL_DIGEST_FILES}
    subject = proposal(files)
    req = rr.seal_request(subject=subject, mode="worker", author_actors=["operator"], nonce="n",
                          authoring_execution_id=EXEC_AUTHOR)  # fmt: skip
    rc = receipt(req)
    assert gate(subject, req, [rc]) == rr.GATE_SATISFIED
    later = rr.proposal_subject(service="mctl-agents", slug="issue-1", gitops_commit=SHA_B, files=files)
    assert gate(later, req, [rc]) == rr.GATE_SATISFIED
    assert gate(proposal({**files, "tasks.md": b"y"}), req, [rc]) == rr.GATE_STALE


def test_gate_unknown_never_decides():
    req = request()
    rc = receipt(req)
    assert gate(None, req, [rc]) == rr.GATE_UNKNOWN
    assert gate(None, req, []) == rr.GATE_UNKNOWN
    assert gate(req.subject, None, [rc]) == rr.GATE_UNKNOWN
    no_exec = request(execution="")
    assert gate(no_exec.subject, no_exec, [receipt(no_exec)]) == rr.GATE_UNKNOWN


def test_gate_independence_rules():
    req = request()
    s = req.subject
    assert gate(s, req, [receipt(req, actor="operator")]) == rr.GATE_NOT_INDEPENDENT
    assert gate(s, req, [receipt(req, execution=EXEC_AUTHOR)]) == rr.GATE_NOT_INDEPENDENT
    # Different actor, shared attempt id or lineage root: still not independent.
    assert gate(s, req, [receipt(req, attempt="at-1")]) == rr.GATE_NOT_INDEPENDENT
    assert gate(s, req, [receipt(req, lineage="trace-1")]) == rr.GATE_NOT_INDEPENDENT
    # A different model name never matters; only actors do.
    assert gate(s, req, [receipt(req, actor="operator")]) == rr.GATE_NOT_INDEPENDENT
    assert gate(s, req, [receipt(req, actor="operator"), receipt(req)]) == rr.GATE_SATISFIED


def test_gate_manual_is_advisory_only_for_independent_gate():
    req = request(mode="manual")
    rc = receipt(req, actor="human:alice")
    assert gate(req.subject, req, [rc]) == rr.GATE_ADVISORY_ONLY
    assert gate(req.subject, req, [rc], assurance="any") == rr.GATE_SATISFIED
    assert gate(req.subject, req, [receipt(req, actor="operator")], assurance="any") == rr.GATE_NOT_INDEPENDENT


def test_gate_latest_receipt_wins_and_outcomes_map():
    req = request()
    crit = [rr.Criterion(c, "unmet") for c in rr.RUBRICS[req.rubric]]
    changes = receipt(req, "CHANGES_REQUESTED", crit=crit)
    passed = receipt(req, attempt="at-10")
    assert gate(req.subject, req, [passed, changes]) == rr.GATE_CHANGES_REQUESTED
    assert gate(req.subject, req, [changes, passed]) == rr.GATE_SATISFIED
    unobs = [rr.Criterion(c, "unobserved") for c in rr.RUBRICS[req.rubric]]
    assert gate(req.subject, req, [receipt(req, "UNOBSERVABLE", crit=unobs)]) == rr.GATE_UNOBSERVABLE
    assert gate(req.subject, req, [receipt(req, "NEEDS_HUMAN", crit=unobs)]) == rr.GATE_NEEDS_HUMAN


def test_regression_amended_spike_with_old_github_approval_is_stale():
    """A PR amended after a `claude[bot]` APPROVED review: the receipt is bound
    to the pre-amendment head and inventory, so GitHub's state is irrelevant."""
    old = request()
    rc = receipt(old)
    amended = spike([rr.InventoryEntry("report.md", b"r amended"), BASE_ENTRIES[1]], head=SHA_A)
    assert gate(amended, old, [rc]) == rr.GATE_STALE


def test_gate_signature_has_no_github_input_and_module_is_isolated():
    import inspect

    params = set(inspect.signature(rr.evaluate_review_gate).parameters)
    assert params == {"live", "request", "receipts", "required_assurance"}
    tree = ast.parse(Path(rr.__file__).read_text())
    imported = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not any(m and "shepherd" in m for m in imported)
    strings = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert not any("[bot]" in v for v in strings)
    assert "GATING_BOTS" not in {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}


def test_gate_request_for_older_binding_is_unknown():
    # Authoring facts sealed for an older revision say nothing about the live one,
    # so a self-review of the live revision must not become `satisfied`.
    old = spike(head=SHA_A)
    live = spike(head=SHA_C)
    old_req = request(old, authors=("operator",))
    live_req = request(live, authors=("operator",))
    self_review = receipt(live_req, actor="operator")
    assert gate(live, old_req, [self_review]) == rr.GATE_UNKNOWN
    assert gate(live, live_req, [self_review]) == rr.GATE_NOT_INDEPENDENT


def test_gate_ignores_receipts_of_another_request():
    req = request()
    other = rr.seal_request(
        subject=req.subject, mode="worker", author_actors=("operator",), nonce="n2",
        authoring_execution_id=EXEC_AUTHOR, authoring_attempt_ids=("at-1",), authoring_lineage_root="trace-1",
    )  # fmt: skip
    assert other.request_id != req.request_id
    assert gate(req.subject, req, [receipt(other)]) == rr.GATE_MISSING
    assert gate(req.subject, req, [receipt(other), receipt(req)]) == rr.GATE_SATISFIED


@pytest.mark.parametrize("field", ["execution", "attempt", "lineage"])
def test_gate_worker_receipt_without_identity_is_not_independent(field):
    req = request()
    assert gate(req.subject, req, [receipt(req, **{field: ""})]) == rr.GATE_NOT_INDEPENDENT


def test_gate_actor_match_is_case_insensitive():
    req = request(authors=("Operator",))
    assert gate(req.subject, req, [receipt(req, actor="operator")]) == rr.GATE_NOT_INDEPENDENT
    assert gate(req.subject, req, [receipt(req, actor="OPERATOR")]) == rr.GATE_NOT_INDEPENDENT


@pytest.mark.parametrize(
    "kwargs",
    [
        {"authors": ("a" * 257,)},
        {"attempts": ("b" * 257,)},
        {"lineage": "c" * 257},
        {"authors": tuple(f"a{i}" for i in range(65))},
    ],
)
def test_request_bounds_hold_in_seal_and_from_dict(kwargs):
    with pytest.raises(rr.ReviewError) as exc:
        request(**kwargs)
    assert exc.value.code == rr.CODE_MALFORMED
    good = request().to_dict()
    key = {"authors": "author_actors", "attempts": "authoring_attempt_ids", "lineage": "authoring_lineage_root"}
    name, value = next(iter(kwargs.items()))
    good[key[name]] = list(value) if name != "lineage" else value
    with pytest.raises(rr.ReviewError) as exc:
        rr.ReviewRequest.from_dict(good)
    assert exc.value.code == rr.CODE_MALFORMED


@pytest.mark.parametrize(
    "form,value,ok",
    [
        ("store-uuid", "we_7ea4cf0c-d8ba-4825-9191-888e17a47eec", True),
        ("legacy-we-16-hex", "we_" + "1" * 16, True),
        ("legacy-we-64-hex", "we_" + "a" * 64, True),
        ("runtime-ex", "ex-0123456789abcdef", True),
        ("store-uuid-uppercase", "we_7EA4CF0C-D8BA-4825-9191-888E17A47EEC", False),
        ("store-uuid-misplaced-hyphen", "we_7ea4cf0cd-8ba-4825-9191-888e17a47eec", False),
        ("store-uuid-braced", "we_{7ea4cf0c-d8ba-4825-9191-888e17a47eec}", False),
        ("store-uuid-urn-prefix", "urn:uuid:7ea4cf0c-d8ba-4825-9191-888e17a47eec", False),
        ("store-uuid-trailing-newline", "we_7ea4cf0c-d8ba-4825-9191-888e17a47eec\n", False),
    ],
)
def test_authoring_execution_id_forms(form, value, ok):
    if ok:
        assert request(execution=value).authoring_execution_id == value, form
    else:
        with pytest.raises(ValueError):
            request(execution=value)
