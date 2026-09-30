"""WorkItem intents as a ContextSnapshot source (mctlhq/mctl-agents#542,
correction tasks 12-17, tests T12-T18).

The investigator-level tests drive `investigate()` against a fake mctl-api
installed at the transport (`WorkItemClient._request`), so the client's
pagination and every classifier run for real; only the HTTP answers are
scripted.
"""
from __future__ import annotations

import ast
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from orchestrator import context_assembly as ca
from orchestrator import context_snapshot as cs
from orchestrator import run_issue_investigator
from orchestrator.run_issue_investigator import IssueData, IssueRef, build_slug, investigate, write_status_yaml
from orchestrator.work_context import client as wc_client
from orchestrator.work_context import execution_requests as xr
from orchestrator.work_context import intents as wi
from orchestrator.work_context import rollout as _rollout
from orchestrator.work_context import snapshots as ws
from tests.test_run_issue_investigator import (
    _dispatched_resume_issue,
    _spy_clone,
    _stub_dispatched_work_item,
)

WID = "wi-542"
REQUEST_ID = "xr_1"
REPO_ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# Fixtures: a scripted mctl-api
# ---------------------------------------------------------------------------


def _intent(iid: int, *, wid: str = WID, text: str | None = None, redacted: bool = False,
            params: Any = None) -> dict[str, Any]:
    return {
        "id": iid,
        "work_item_id": wid,
        "actor_principal": "user:mashkovd",
        "surface": "telegram",
        "text": "" if redacted else (text if text is not None else f"please do step {iid}"),
        "text_redacted": redacted,
        "params": params if params is not None else {"step": iid},
        "created_at": f"2026-09-30T10:{iid % 60:02d}:00Z",
    }


class _FakeStore:
    """mctl-api's intent and execution-request routes, scripted per test.
    Every other route is unreachable (`WorkItemUnavailable`), which is what
    the no-token fixture gives the rest of the work-context conversation."""

    def __init__(self, *, intents: list[dict[str, Any]] | None = None, intent_id: int | None = None,
                 request_work_item_id: str = WID) -> None:
        self.intents = list(intents or [])
        self.intent_id = intent_id
        self.request_work_item_id = request_work_item_id
        self.request_answer: tuple[int, dict[str, Any]] | None = None
        self.read_answer: tuple[int, dict[str, Any]] | None = None
        self.listing_answer: tuple[int, dict[str, Any]] | None = None
        self.paths: list[str] = []

    def intent_paths(self) -> list[str]:
        return [p for p in self.paths if "/intents" in p]

    def handle(self, method: str, path: str) -> wc_client._HTTPResult:
        self.paths.append(path)
        prefix = f"/api/v1/work-items/{WID}"
        if path == f"{prefix}/execution-requests/{REQUEST_ID}":
            if self.request_answer is not None:
                return wc_client._HTTPResult(*self.request_answer)
            request: dict[str, Any] = {
                "id": REQUEST_ID, "work_item_id": self.request_work_item_id, "kind": "resume",
                "state": xr.STATE_CLAIMED,
            }
            if self.intent_id is not None:
                request["intent_id"] = self.intent_id
            return wc_client._HTTPResult(200, {"schema_version": wi.SCHEMA_VERSION, "execution_request": request})
        if path.startswith(f"{prefix}/intents?"):
            if self.listing_answer is not None:
                return wc_client._HTTPResult(*self.listing_answer)
            query = dict(part.split("=") for part in path.split("?", 1)[1].split("&"))
            after, limit = int(query["after_id"]), int(query["limit"])
            newer = [i for i in self.intents if i["id"] > after]
            return wc_client._HTTPResult(200, {
                "schema_version": wi.SCHEMA_VERSION, "intents": newer[:limit],
                "truncated": len(newer) > limit, "limit": limit,
            })
        if path.startswith(f"{prefix}/intents/"):
            if self.read_answer is not None:
                return wc_client._HTTPResult(*self.read_answer)
            iid = int(path.rsplit("/", 1)[1])
            for i in self.intents:
                if i["id"] == iid:
                    return wc_client._HTTPResult(200, {"schema_version": wi.SCHEMA_VERSION, "intent": i})
            return wc_client._HTTPResult(404, {"code": wi.INTENT_NOT_FOUND_CODE})
        raise wc_client.WorkItemUnavailable(f"not scripted: {method} {path}")


def _install(monkeypatch, store: _FakeStore) -> _FakeStore:
    monkeypatch.setenv("MCTL_TOKEN", "test-token")
    monkeypatch.delenv("MCTL_API_BASE_URL", raising=False)
    monkeypatch.setattr(
        wc_client.WorkItemClient, "_request", lambda self, method, path, payload=None: store.handle(method, path)
    )
    return store


def _env(monkeypatch, *, switch: str | None) -> None:
    monkeypatch.setenv(_rollout.ENV_VAR, _rollout.OBSERVE)
    monkeypatch.delenv(_rollout.REQUIRED_ENV_VAR, raising=False)
    monkeypatch.setenv("ISSUE_INVESTIGATOR_CONTEXT_MODE", "shadow")
    if switch is None:
        monkeypatch.delenv(wi.SWITCH_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(wi.SWITCH_ENV_VAR, switch)


def _record_persist(monkeypatch) -> list[cs.ContextSnapshot]:
    persisted: list[cs.ContextSnapshot] = []

    def _persist(snapshot, client):
        persisted.append(snapshot)
        return ws.SnapshotAnswer(
            ws.SNAPSHOT_SEALED, snapshot_id="cs_test0000000000000000000000000000", content_hash="deadbeef"
        )

    monkeypatch.setattr(ws, "persist", _persist)
    return persisted


def _capture_assembly(monkeypatch) -> list[ca.AssemblyResult]:
    captured: list[ca.AssemblyResult] = []
    real = run_issue_investigator.context_assembly.assemble_investigator_context

    def _capturing(**kwargs):
        result = real(**kwargs)
        if result is not None:
            captured.append(result)
        return result

    monkeypatch.setattr(run_issue_investigator.context_assembly, "assemble_investigator_context", _capturing)
    return captured


def _terminal_proposal(tmp_path: Path, issue, number: int, status: str) -> Path:
    slug = build_slug(number, "Fix work context resume")
    proposal_dir = tmp_path / "mctl-telegram" / "proposals" / slug
    write_status_yaml(proposal_dir, issue)
    (proposal_dir / ".status.yaml").write_text(yaml.safe_dump({"status": status}))
    return proposal_dir


def _tree(directory: Path) -> dict[Path, bytes]:
    return {p: p.read_bytes() for p in directory.rglob("*") if p.is_file()}


def _run_resume(tmp_path: Path, issue) -> Any:
    return investigate(
        issue.ref.url,
        state_dir=tmp_path,
        work_item_id=WID,
        execution_id="we_dispatch",
        execution_request_id=REQUEST_ID,
        resume_from_execution_id="we_prior",
    )


def _intent_sources(snapshot: cs.ContextSnapshot) -> list[dict[str, Any]]:
    return [s for s in snapshot.to_dict()["sources"] if s["kind"] == "work-item-intent"]


# ---------------------------------------------------------------------------
# T12 / T13: the resume intent reaches C2 on the context-only path
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("existing_status", ["accepted", "implemented", "merged"])
def test_t12_context_only_resume_carries_the_intent_and_leaves_the_proposal_byte_identical(
    tmp_path, monkeypatch, capsys, existing_status
):
    _env(monkeypatch, switch="on")
    store = _install(monkeypatch, _FakeStore(intents=[_intent(3), _intent(7, text="ship the digest")], intent_id=7))
    _stub_dispatched_work_item(monkeypatch)
    persisted = _record_persist(monkeypatch)
    captured = _capture_assembly(monkeypatch)
    issue = _dispatched_resume_issue(monkeypatch)
    _spy_clone(monkeypatch, tmp_path)
    proposal_dir = _terminal_proposal(tmp_path, issue, 542, existing_status)
    before = _tree(proposal_dir)

    result = _run_resume(tmp_path, issue)

    assert result.error is None
    assert result.context_only is True
    assert result.outcome_code == "succeeded"
    assert _tree(proposal_dir) == before
    assert len(persisted) == 1

    sources = {s["source_id"]: s for s in _intent_sources(captured[0].snapshot)}
    pinned = sources[f"work-item-intent:{WID}:7"]
    assert pinned["selector"] == {
        "work_item_id": WID, "intent_id": 7, "actor_principal": "user:mashkovd", "surface": "telegram",
        "created_at": "2026-09-30T10:07:00Z", "text_redacted": False, "fields": "text,params",
    }
    want = cs.canonical_json({"text": "ship the digest", "params": {"step": 7}})
    assert pinned["content_hash"] == cs.hash_bytes(want)
    assert pinned["trust"]["tier"] == "reported"
    assert pinned["selection"]["reason_code"] == "resume-intent"
    assert pinned["locator"] == f"https://api.mctl.ai/api/v1/work-items/{WID}/intents/7"
    # The older intent is there too, as an ordinary (unpinned) source.
    assert sources[f"work-item-intent:{WID}:3"]["selection"]["reason_code"] == "work-item-intent"
    assert f"/api/v1/work-items/{WID}/intents/7" in store.paths
    assert "[context] work-item-intent source=on listed=2 resume_intent_id=7" in capsys.readouterr().out


def test_t13_intent_is_present_although_the_issue_has_no_comments(tmp_path, monkeypatch):
    """The resume intent is in C2 even though the issue-comments collector
    has nothing to give: it no longer depends on a GitHub comment."""
    _env(monkeypatch, switch="on")
    _install(monkeypatch, _FakeStore(intents=[_intent(9)], intent_id=9))
    _stub_dispatched_work_item(monkeypatch)
    _record_persist(monkeypatch)
    captured = _capture_assembly(monkeypatch)
    issue = _dispatched_resume_issue(monkeypatch, number=560)
    assert not getattr(issue, "comments", ())
    _spy_clone(monkeypatch, tmp_path)
    _terminal_proposal(tmp_path, issue, 560, "merged")

    result = _run_resume(tmp_path, issue)

    assert result.outcome_code == "succeeded"
    kinds = [s["kind"] for s in captured[0].snapshot.to_dict()["sources"]]
    assert "issue-comment" not in kinds
    assert [s["source_id"] for s in _intent_sources(captured[0].snapshot)] == [f"work-item-intent:{WID}:9"]


# ---------------------------------------------------------------------------
# T14: an unresolved resume intent fails the run and persists nothing
# ---------------------------------------------------------------------------


_UNRESOLVED_READS = {
    "404-intent-not-found": (404, {"code": wi.INTENT_NOT_FOUND_CODE}),
    "other-item": (200, {"schema_version": wi.SCHEMA_VERSION, "intent": _intent(7, wid="wi-other")}),
    "5xx": (503, {"code": "unavailable"}),
    "malformed": (200, {"schema_version": wi.SCHEMA_VERSION, "intent": {"id": 7}}),
}


@pytest.mark.parametrize("case", sorted(_UNRESOLVED_READS))
@pytest.mark.parametrize("path", ["context-only", "full"])
def test_t14_unresolved_resume_intent_fails_and_persists_nothing(tmp_path, monkeypatch, capsys, case, path):
    _env(monkeypatch, switch="on")
    store = _FakeStore(intents=[_intent(7)], intent_id=7)
    store.read_answer = _UNRESOLVED_READS[case]
    _install(monkeypatch, store)
    _stub_dispatched_work_item(monkeypatch)
    persisted = _record_persist(monkeypatch)
    number = 561
    issue = _dispatched_resume_issue(monkeypatch, number=number)
    _spy_clone(monkeypatch, tmp_path)
    proposal_dir = tmp_path / "mctl-telegram" / "proposals" / build_slug(number, "Fix work context resume")
    if path == "context-only":
        _terminal_proposal(tmp_path, issue, number, "merged")
        before = _tree(proposal_dir)
    else:
        before = {}

    result = _run_resume(tmp_path, issue)

    assert result.outcome_code == "failed"
    assert result.outcome_reason == "intent-unresolved"
    assert result.error is not None
    assert result.context_only is (path == "context-only")
    assert persisted == []
    if path == "context-only":
        assert _tree(proposal_dir) == before
    out = capsys.readouterr().out
    assert "[outcome] code=failed reason=intent-unresolved" in out


def test_t14_unreadable_execution_request_is_unresolved(tmp_path, monkeypatch):
    _env(monkeypatch, switch="on")
    store = _FakeStore(intents=[_intent(7)], intent_id=7)
    store.request_answer = (500, {"code": "internal"})
    _install(monkeypatch, store)
    _stub_dispatched_work_item(monkeypatch)
    persisted = _record_persist(monkeypatch)
    issue = _dispatched_resume_issue(monkeypatch, number=562)
    _spy_clone(monkeypatch, tmp_path)
    _terminal_proposal(tmp_path, issue, 562, "merged")

    result = _run_resume(tmp_path, issue)

    assert (result.outcome_code, result.outcome_reason) == ("failed", "intent-unresolved")
    assert persisted == []
    assert store.intent_paths() == []


def test_t14_failed_listing_is_unresolved(tmp_path, monkeypatch):
    _env(monkeypatch, switch="on")
    store = _FakeStore(intents=[_intent(7)], intent_id=None)
    store.listing_answer = (502, {})
    _install(monkeypatch, store)
    _stub_dispatched_work_item(monkeypatch)
    persisted = _record_persist(monkeypatch)
    issue = _dispatched_resume_issue(monkeypatch, number=563)
    _spy_clone(monkeypatch, tmp_path)
    _terminal_proposal(tmp_path, issue, 563, "merged")

    result = _run_resume(tmp_path, issue)

    assert (result.outcome_code, result.outcome_reason) == ("failed", "intent-unresolved")
    assert persisted == []


def test_main_exits_nonzero_on_intent_unresolved(monkeypatch):
    def _unresolved(*args, **kwargs):
        return run_issue_investigator.InvestigateResult(
            "svc", "slug", Path("/nonexistent"), error="WorkItem intent unresolved: x",
            outcome_code="failed", outcome_reason="intent-unresolved", context_only=True,
        )

    monkeypatch.setattr(run_issue_investigator, "investigate", _unresolved)
    monkeypatch.setattr(
        "sys.argv", ["run_issue_investigator", "--issue-url", "https://github.com/mctlhq/mctl-telegram/issues/1"]
    )
    with pytest.raises(SystemExit) as exc:
        run_issue_investigator.main()
    assert exc.value.code not in (0, None)


def test_request_without_intent_id_pins_nothing(tmp_path, monkeypatch):
    _env(monkeypatch, switch="on")
    store = _install(monkeypatch, _FakeStore(intents=[_intent(4)], intent_id=None))
    _stub_dispatched_work_item(monkeypatch)
    _record_persist(monkeypatch)
    captured = _capture_assembly(monkeypatch)
    issue = _dispatched_resume_issue(monkeypatch, number=564)
    _spy_clone(monkeypatch, tmp_path)
    _terminal_proposal(tmp_path, issue, 564, "merged")

    result = _run_resume(tmp_path, issue)

    assert result.outcome_code == "succeeded"
    assert not any("/intents/" in p for p in store.paths)
    sources = _intent_sources(captured[0].snapshot)
    assert [s["selection"]["reason_code"] for s in sources] == ["work-item-intent"]


# ---------------------------------------------------------------------------
# text_redacted
# ---------------------------------------------------------------------------


def test_redacted_resume_intent_is_carried_with_text_redacted_in_provenance(tmp_path, monkeypatch):
    _env(monkeypatch, switch="on")
    _install(monkeypatch, _FakeStore(intents=[_intent(5, redacted=True)], intent_id=5))
    _stub_dispatched_work_item(monkeypatch)
    _record_persist(monkeypatch)
    captured = _capture_assembly(monkeypatch)
    issue = _dispatched_resume_issue(monkeypatch, number=565)
    _spy_clone(monkeypatch, tmp_path)
    _terminal_proposal(tmp_path, issue, 565, "merged")

    result = _run_resume(tmp_path, issue)

    assert result.outcome_code == "succeeded"
    (source,) = _intent_sources(captured[0].snapshot)
    assert source["selector"]["text_redacted"] is True
    assert source["content_hash"] == cs.hash_bytes(cs.canonical_json({"text": "", "params": {"step": 5}}))
    rendered = "\n".join(captured[0].rendered.values())
    assert "[text removed by retention]" in rendered


def test_redacted_and_empty_text_are_distinct_sources():
    empty = wi.Intent.from_payload(_intent(5, text=""))
    redacted = wi.Intent.from_payload(_intent(5, redacted=True))
    assert empty is not None and redacted is not None
    a = ca._intent_candidate(_assembly_input(Path("/tmp")), empty, pinned=False)
    b = ca._intent_candidate(_assembly_input(Path("/tmp")), redacted, pinned=False)
    assert a.raw == b.raw  # the text is gone either way ...
    assert a.selector["text_redacted"] is False  # ... but provenance says which
    assert b.selector["text_redacted"] is True
    assert "[text removed by retention]" in b.render_text
    assert "[text removed by retention]" not in a.render_text


# ---------------------------------------------------------------------------
# T15: determinism
# ---------------------------------------------------------------------------


def _assembly_input(tmp_path: Path, **overrides: Any) -> ca.AssemblyInput:
    issue = IssueData(
        ref=IssueRef(owner="mctlhq", repo="mctl-telegram", number=542,
                     url="https://github.com/mctlhq/mctl-telegram/issues/542"),
        title="Fix work context resume", body="Body text", state="OPEN",
    )
    fields: dict[str, Any] = {
        "issue": issue, "repo_dir": tmp_path / "repo", "target_repo_sha": "a" * 40,
        "full_repo": "mctlhq/mctl-telegram", "proposal_dir": tmp_path / "proposal", "service": "mctl-telegram",
        "slug": "issue-542-x", "prompt_template": "Investigate {issue}",
        "now": datetime(2026, 9, 30, 12, 0, tzinfo=UTC), "config": ca.AssemblyConfig(),
    }
    fields.update(overrides)
    return ca.AssemblyInput(**fields)


def _execution() -> Any:
    return ca.build_execution_correlation(
        resolver_mode="legacy",
        issue_url="https://github.com/mctlhq/mctl-telegram/issues/542",
        target_repository_sha="a" * 40,
        legacy_model="claude-sonnet-5",
    )


def _parsed(*ids: int) -> tuple[wi.Intent, ...]:
    out = tuple(wi.Intent.from_payload(_intent(i)) for i in ids)
    assert all(out)
    return out  # type: ignore[return-value]


def test_t15_same_intents_seal_the_same_snapshot(tmp_path):
    (tmp_path / "repo").mkdir()
    intents = _parsed(2, 4, 6)

    def _seal(order: tuple[wi.Intent, ...]) -> bytes:
        inp = _assembly_input(tmp_path, work_item_intents=order, resume_intent=intents[1])
        result = ca.assemble(inp, mode="shadow", execution=_execution())
        return cs.canonical_json(result.snapshot.to_dict())

    first = _seal(intents)
    assert _seal(intents) == first
    # The listing's arrival order does not change the selection or the bytes.
    assert _seal(tuple(reversed(intents))) == first


def test_select_takes_newer_than_high_water_ascending_capped_plus_pinned():
    intents = _parsed(*range(1, 41))
    pinned, ranked = ca.select_work_item_intents(
        tuple(reversed(intents)), prior_high_water=10, resume_intent=intents[4]
    )
    assert pinned is intents[4]
    assert [i.intent_id for i in ranked] == list(range(11, 11 + ca.MAX_WORK_ITEM_INTENTS))
    # The pinned intent is never repeated among the ranked ones.
    _, ranked2 = ca.select_work_item_intents(intents, prior_high_water=None, resume_intent=intents[0])
    assert 1 not in [i.intent_id for i in ranked2]
    assert [i.intent_id for i in ranked2] == list(range(2, 2 + ca.MAX_WORK_ITEM_INTENTS))


def test_pinned_intent_survives_a_budget_that_drops_the_ranked_ones(tmp_path):
    (tmp_path / "repo").mkdir()
    intents = _parsed(1, 2, 3)
    inp = _assembly_input(tmp_path, work_item_intents=intents, resume_intent=intents[2])
    full = ca.assemble(inp, mode="shadow", execution=_execution())
    selected = [s.source_id for s in full.snapshot.sources if s.kind == "work-item-intent"]
    assert selected[0] == f"work-item-intent:{WID}:3"
    # The pinned intent ranks right after the issue in the default strategy.
    order = [s.kind for s in full.snapshot.sources]
    assert order.index("work-item-intent") == order.index("github-issue") + 1


# ---------------------------------------------------------------------------
# T16: nothing that authorizes or approves reads the intents
# ---------------------------------------------------------------------------


# context_snapshot.py only declares the kind in SOURCE_KINDS; intents.py is
# the module itself and is not scanned against itself.
_INTENT_READERS = {
    "orchestrator/context_assembly.py",
    "orchestrator/context_snapshot.py",
    "orchestrator/work_context/client.py",
}


def _references_intents(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "orchestrator.work_context.intents":
                return True
            if node.module == "orchestrator.work_context" and any(a.name == "intents" for a in node.names):
                return True
        if isinstance(node, ast.Import) and any(a.name == "orchestrator.work_context.intents" for a in node.names):
            return True
        if isinstance(node, ast.Attribute) and node.attr in {"list_intents", "work_item_intent", "work_item_intents",
                                                             "resume_intent"}:
            return True
        if isinstance(node, ast.Constant) and node.value == "work-item-intent":
            return True
    return False


def test_t16_only_context_assembly_reads_the_intents():
    """No policy, capability, approval, lifecycle or proposal-state module
    reads an intent: they are provenance and input, never authorization.
    `run_issue_investigator.py` only maps the typed failure."""
    readers = set()
    for path in sorted((REPO_ROOT / "orchestrator").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT).as_posix()
        if _references_intents(ast.parse(path.read_text(), filename=rel)):
            readers.add(rel)
    assert readers == _INTENT_READERS


def test_t16_the_detector_sees_a_reader():
    assert _references_intents(ast.parse("from orchestrator.work_context import intents\n"))
    assert _references_intents(ast.parse("x = client.list_intents(w)\n"))
    assert _references_intents(ast.parse("k = 'work-item-intent'\n"))
    assert not _references_intents(ast.parse("x = client.get(w)\n"))


# ---------------------------------------------------------------------------
# T18: the switch
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("switch", ["off", None, "bogus"])
def test_t18_switch_off_unset_or_bogus_makes_no_intent_request_and_keeps_bytes(
    tmp_path, monkeypatch, capsys, switch
):
    def _run(sw: str | None) -> tuple[bytes, _FakeStore, str]:
        _env(monkeypatch, switch=sw)
        store = _install(monkeypatch, _FakeStore(intents=[_intent(7)], intent_id=7))
        _stub_dispatched_work_item(monkeypatch)
        _record_persist(monkeypatch)
        captured = _capture_assembly(monkeypatch)
        work = tmp_path / (sw or "unset") / str(len(list(tmp_path.iterdir())))
        issue = _dispatched_resume_issue(monkeypatch, number=566)
        _spy_clone(monkeypatch, work)
        _terminal_proposal(work, issue, 566, "merged")
        monkeypatch.setattr(ca, "datetime", _FrozenDatetime)
        result = _run_resume(work, issue)
        assert result.outcome_code == "succeeded"
        doc = captured[0].snapshot.to_dict()
        # Today's collectors, no more: the intent collectors do not even run.
        assert captured[0].metrics.collector_calls == len(ca._COLLECTOR_ORDER)
        return _stable(doc), store, capsys.readouterr().out

    got, store, out = _run(switch)
    assert store.intent_paths() == []
    assert "[context] work-item-intent source=off" in out
    if switch == "bogus":
        assert f"warn: {wi.SWITCH_ENV_VAR}='bogus'" in out
    assert b"work-item-intent" not in got

    # Byte-identical to the same run with the intent code path removed.
    monkeypatch.setattr(ca, "_resolve_work_item_intents", lambda *a, **k: ((), None, None))
    baseline, _, _ = _run("on")
    assert got == baseline


class _FrozenDatetime(datetime):
    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 9, 30, 12, 0, tzinfo=tz or UTC)


def _stable(doc: dict[str, Any]) -> bytes:
    """The snapshot minus the paths that name the per-run tmp directory."""
    return json.dumps(doc, sort_keys=True, default=str).replace(
        str(Path.cwd()), "<cwd>"
    ).encode()


def test_t18_prior_snapshot_without_the_kind_selects_every_intent_up_to_the_cap():
    doc = {"sources": [{"kind": "github-issue", "selector": {}}]}
    assert ca.prior_intent_high_water(doc) is None
    assert ca.prior_intent_high_water(None) is None
    intents = _parsed(*range(1, 31))
    _, ranked = ca.select_work_item_intents(intents, prior_high_water=ca.prior_intent_high_water(doc),
                                            resume_intent=None)
    assert [i.intent_id for i in ranked] == list(range(1, 1 + ca.MAX_WORK_ITEM_INTENTS))


def test_prior_snapshot_with_the_kind_sets_the_high_water_mark():
    doc = {"sources": [
        {"kind": "work-item-intent", "selector": {"intent_id": 4}},
        {"kind": "work-item-intent", "selector": {"intent_id": 9}},
        {"kind": "issue-comment", "selector": {"intent_id": 99}},
    ]}
    assert ca.prior_intent_high_water(doc) == 9


@pytest.mark.parametrize(
    ("raw", "want"),
    [(None, "off"), ("", "off"), ("off", "off"), ("OFF", "off"), ("on", "on"), (" On ", "on"),
     ("true", "off"), ("1", "off")],
)
def test_switch_values(monkeypatch, raw, want):
    if raw is None:
        monkeypatch.delenv(wi.SWITCH_ENV_VAR, raising=False)
    else:
        monkeypatch.setenv(wi.SWITCH_ENV_VAR, raw)
    assert wi.switch() == want


# ---------------------------------------------------------------------------
# Classifiers and the client: "could not observe" is never "observed absent"
# ---------------------------------------------------------------------------


def _listing(intents: list[dict[str, Any]], truncated: bool = False) -> dict[str, Any]:
    return {"schema_version": wi.SCHEMA_VERSION, "intents": intents, "truncated": truncated, "limit": 100}


@pytest.mark.parametrize(
    ("status", "payload", "verdict"),
    [
        (404, {"code": wi.WORK_ITEM_NOT_FOUND_CODE}, wi.INTENT_ABSENT),
        (404, {"code": "route_not_found"}, wi.INTENTS_UNKNOWN),
        (500, {}, wi.INTENTS_UNKNOWN),
        (200, {"intents": [], "truncated": False}, wi.INTENTS_UNKNOWN),
        (200, {"schema_version": wi.SCHEMA_VERSION, "truncated": False}, wi.INTENTS_UNKNOWN),
        (200, {"schema_version": wi.SCHEMA_VERSION, "intents": []}, wi.INTENTS_UNKNOWN),
        (200, _listing([{"id": 1}]), wi.INTENTS_UNKNOWN),
        (200, _listing([_intent(1, wid="wi-other")]), wi.INTENTS_UNKNOWN),
        (200, _listing([_intent(2), _intent(1)]), wi.INTENTS_UNKNOWN),
        (200, _listing([], truncated=True), wi.INTENTS_UNKNOWN),
        (200, _listing([{**_intent(1), "text_redacted": None}]), wi.INTENTS_UNKNOWN),
    ],
)
def test_page_classifier(status, payload, verdict):
    page, terminal = wi.page_from(status, payload, work_item_id=WID, after_id=0)
    assert page is None
    assert terminal is not None and terminal.verdict == verdict


def test_page_classifier_accepts_an_empty_listing_as_documented_absence():
    page, terminal = wi.page_from(200, _listing([]), work_item_id=WID, after_id=0)
    assert terminal is None and page is not None
    assert page.intents == () and page.truncated is False


@pytest.mark.parametrize(
    ("status", "payload", "verdict"),
    [
        (404, {"code": wi.INTENT_NOT_FOUND_CODE}, wi.INTENT_ABSENT),
        (404, {"code": wi.WORK_ITEM_NOT_FOUND_CODE}, wi.INTENT_ABSENT),
        (404, {}, wi.INTENTS_UNKNOWN),
        (502, {}, wi.INTENTS_UNKNOWN),
        (200, {"intent": _intent(7)}, wi.INTENTS_UNKNOWN),
        (200, {"schema_version": wi.SCHEMA_VERSION, "intent": {"id": 7}}, wi.INTENTS_UNKNOWN),
        (200, {"schema_version": wi.SCHEMA_VERSION, "intent": _intent(8)}, wi.INTENTS_UNKNOWN),
        (200, {"schema_version": wi.SCHEMA_VERSION, "intent": _intent(7, wid="wi-other")}, wi.INTENTS_UNKNOWN),
        (200, {"schema_version": wi.SCHEMA_VERSION, "intent": _intent(7)}, wi.INTENT_FOUND),
    ],
)
def test_read_classifier(status, payload, verdict):
    assert wi.answer_from_read(status, payload, work_item_id=WID, intent_id=7).verdict == verdict


def test_client_paginates_with_after_id_until_not_truncated(monkeypatch):
    store = _install(monkeypatch, _FakeStore(intents=[_intent(i) for i in range(1, 251)]))
    answer = wc_client.WorkItemClient().list_intents(WID)
    assert answer.verdict == wi.INTENTS_LISTED
    assert [i.intent_id for i in answer.intents] == list(range(1, 251))
    assert store.intent_paths() == [
        f"/api/v1/work-items/{WID}/intents?after_id=0&limit=100",
        f"/api/v1/work-items/{WID}/intents?after_id=100&limit=100",
        f"/api/v1/work-items/{WID}/intents?after_id=200&limit=100",
    ]


def test_client_failed_continuation_is_unknown_not_a_shorter_list(monkeypatch):
    store = _FakeStore(intents=[_intent(i) for i in range(1, 151)])
    real = store.handle

    def _flaky(method, path):
        if "after_id=100" in path:
            store.paths.append(path)
            return wc_client._HTTPResult(503, {})
        return real(method, path)

    store.handle = _flaky  # type: ignore[method-assign]
    _install(monkeypatch, store)
    assert wc_client.WorkItemClient().list_intents(WID).verdict == wi.INTENTS_UNKNOWN


def test_client_item_vanishing_mid_listing_is_unknown(monkeypatch):
    store = _FakeStore(intents=[_intent(i) for i in range(1, 151)])
    real = store.handle

    def _vanishing(method, path):
        if "after_id=100" in path:
            return wc_client._HTTPResult(404, {"code": wi.WORK_ITEM_NOT_FOUND_CODE})
        return real(method, path)

    store.handle = _vanishing  # type: ignore[method-assign]
    _install(monkeypatch, store)
    assert wc_client.WorkItemClient().list_intents(WID).verdict == wi.INTENTS_UNKNOWN


def test_client_listing_longer_than_max_pages_is_unknown(monkeypatch):
    monkeypatch.setattr(wi, "MAX_PAGES", 2)
    _install(monkeypatch, _FakeStore(intents=[_intent(i) for i in range(1, 251)]))
    assert wc_client.WorkItemClient().list_intents(WID).verdict == wi.INTENTS_UNKNOWN


def test_client_transport_error_is_unknown(monkeypatch):
    monkeypatch.delenv("MCTL_TOKEN", raising=False)
    client = wc_client.WorkItemClient()
    assert client.list_intents(WID).verdict == wi.INTENTS_UNKNOWN
    assert client.work_item_intent(WID, 7).verdict == wi.INTENTS_UNKNOWN


def test_routes_are_declared():
    assert wc_client.ROUTES["list_intents"] == "/api/v1/work-items/{id}/intents"
    assert wc_client.ROUTES["work_item_intent"] == "/api/v1/work-items/{id}/intents/{intent_id}"
    assert "work-item-intent" in cs.SOURCE_KINDS


@pytest.mark.parametrize(
    ("value", "parses", "want"),
    [("absent", True, None), (None, True, None), (7, True, 7), (0, False, None), (-1, False, None),
     ("7", False, None), (True, False, None), (7.0, False, None)],
)
def test_execution_request_intent_id(value, parses, want):
    data: dict[str, Any] = {"id": REQUEST_ID, "work_item_id": WID, "kind": "resume", "state": xr.STATE_CLAIMED}
    if value != "absent":
        data["intent_id"] = value
    request = xr.ExecutionRequest.from_payload(data)
    if not parses:
        assert request is None
    else:
        assert request is not None and request.intent_id == want


def test_intent_from_payload_requires_text_redacted():
    data = _intent(1)
    del data["text_redacted"]
    assert wi.Intent.from_payload(data) is None
    assert wi.Intent.from_payload({**_intent(1), "id": True}) is None
    assert wi.Intent.from_payload(_intent(1)) is not None
