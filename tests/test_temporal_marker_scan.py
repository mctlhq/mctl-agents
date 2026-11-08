"""Tests for tools/temporal_marker_scan.py (mctl-agents#1007, P-6).

A detector is accepted only when it is green on converged input AND red on a
deliberate mutation (AGENTS.md). So this file has three layers:

1. `detector_checks(mod)`: every assertion that defines a correct scanner,
   written against a module object so it can be pointed at any variant.
2. Plain tests: the real tool passes `detector_checks`, the ported offline
   classifier tests, a fake-client listing/pagination fault injection, and an
   integration test on REAL histories produced by the Temporal time-skipping
   test server.
3. Mutation tests: each mutation rewrites one snippet of the tool source into
   a temp module and requires `detector_checks` to FAIL against it. Each
   mutation also asserts its snippet exists, so a refactor cannot silently turn
   a mutation into a no-op (that would be a detector that can only pass).
"""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import sys
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from temporalio import workflow

_REPO = Path(__file__).resolve().parent.parent
_TOOL = _REPO / "tools" / "temporal_marker_scan.py"
_FIX = _REPO / "tests" / "fixtures" / "histories"


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(mod)
    return mod


ms = _load(_TOOL, "temporal_marker_scan")
POS = (_FIX / "dev_loop_deferred_resume.json").read_text()
LEGACY = ["dev_loop_full.prepatch.json", "dev_loop_issue_keyed.json", "dev_loop_dispatched.json",
          "dev_loop_resumed.json", "dev_loop_parked.json", "dev_loop_delegated.json",
          "dev_loop_full.patched.json", "dev_loop_issue_started_delegated.json"]


def _fx(name: str) -> str:
    return (_FIX / name).read_text()


def _is_target_marker(e: dict[str, Any]) -> bool:
    m = e.get("markerRecordedEventAttributes") or {}
    if m.get("markerName") != "core_patch":
        return False
    payloads = m["details"]["patch-data"]["payloads"]
    return any(json.loads(base64.b64decode(p["data"]))["id"] == ms.PATCH for p in payloads)


def _marker_event(eid: int, patch_id: str) -> dict[str, Any]:
    data = base64.b64encode(json.dumps({"id": patch_id, "deprecated": False}).encode()).decode()
    return {"eventId": str(eid), "eventType": "EVENT_TYPE_MARKER_RECORDED",
            "markerRecordedEventAttributes": {"markerName": "core_patch",
                                              "details": {"patch-data": {"payloads": [{"data": data}]}}}}


def _synthetic(*, marker: str | None = None, continued_from: str | None = None, n: int = 4) -> dict[str, Any]:
    attrs: dict[str, Any] = {"continuedExecutionRunId": continued_from} if continued_from else {}
    evs: list[dict[str, Any]] = [{"eventId": "1", "eventType": "EVENT_TYPE_WORKFLOW_EXECUTION_STARTED",
                                  "workflowExecutionStartedEventAttributes": attrs}]
    for i in range(2, n + 1):
        evs.append({"eventId": str(i), "eventType": "EVENT_TYPE_WORKFLOW_TASK_SCHEDULED"})
    if marker:
        evs.append(_marker_event(len(evs) + 1, marker))
    return {"events": evs}


class FakeClient:
    """Fake listing/fetching. `pages` is a list of pages; an Exception entry raises there."""

    def __init__(self, pages: list[Any], histories: dict[str, Any]):
        self.pages, self.histories, self.fetched = pages, histories, []

    def list_fn(self, query: str) -> AsyncIterator[tuple[str, str, str]]:
        async def gen() -> AsyncIterator[tuple[str, str, str]]:
            for page in self.pages:
                if isinstance(page, Exception):
                    raise page
                for wid in page:
                    yield wid, "run-" + wid, "DevLoopWorkflow"
        return gen()

    async def fetch_fn(self, wid: str, run_id: str) -> Any:
        self.fetched.append(wid)
        h = self.histories[f"{wid}@{run_id}"] if f"{wid}@{run_id}" in self.histories else self.histories[wid]
        if isinstance(h, Exception):
            raise h
        return h


def _scan(mod: ModuleType, client: FakeClient) -> Any:
    return asyncio.run(mod.scan(client.list_fn, client.fetch_fn))


FIRST_RUN_PATCH = "merge-watch-terminal-reconcile"  # recorded in the first run of a watch, not on hops


def _cli_with_patch_id(mod: ModuleType, history: dict[str, Any], extra: list[str]) -> tuple[int, dict[str, Any]]:
    """Run main() against a one-workflow fake client; return (exit code, JSON payload)."""
    import contextlib
    import io

    class Hist:
        def to_json(self) -> str:
            return json.dumps(history)

    class Handle:
        async def fetch_history(self) -> Hist:
            return Hist()

    class Wf:
        id, run_id, workflow_type = "w", "r", "DevLoopWorkflow"

    class C:
        def list_workflows(self, query: str) -> AsyncIterator[Wf]:
            async def gen() -> AsyncIterator[Wf]:
                yield Wf()
            return gen()

        def get_workflow_handle(self, wid: str, run_id: str | None = None) -> Handle:
            return Handle()

        async def count_workflows(self, query: str) -> Any:
            return type("Count", (), {"count": 1})()

    async def connect(address: str, namespace: str, **options: Any) -> Any:
        return C()

    saved, mod._connect = mod._connect, connect
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            try:
                code = mod.main(["--address", "x:1", "--namespace", "n", "--json", *extra])
            except SystemExit as exc:  # an option the tool does not know is a failed check, not a crash
                raise AssertionError(f"main() exited with {exc.code} for {extra}") from exc
    finally:
        mod._connect = saved
    return code, json.loads(buf.getvalue())


def _patch_id_checks(mod: ModuleType) -> None:
    """--patch-id picks the id, the default is unchanged, and the report names the id."""
    A, P, U = mod.ABSENT, mod.PRESENT, mod.UNKNOWN
    new = FIRST_RUN_PATCH
    assert mod.PATCH == "deferred-resume-contract"
    clean = json.dumps(_synthetic())
    with_new = json.dumps(_synthetic(marker=new))
    with_default = json.dumps(_synthetic(marker=mod.PATCH))
    gap = _synthetic()
    del gap["events"][2]

    def verdicts(patch: str | None, hist: dict[str, Any]) -> dict[str, str]:
        client = FakeClient([sorted(k for k in hist if "@" not in k)], hist)
        kw = {} if patch is None else {"patch": patch}
        r = asyncio.run(mod.scan(client.list_fn, client.fetch_fn, **kw))
        return {e.workflow_id: e.verdict for e in r.entries}

    hist = {"new": with_new, "none": clean, "default": with_default, "gap": json.dumps(gap), "bad": "{"}
    # an explicit id: present / absent (including a different patch) / unknown
    assert verdicts(new, hist) == {"new": P, "none": A, "default": A, "gap": U, "bad": U}
    # the default id is unchanged and does not see the other one
    want_default = {"new": A, "none": A, "default": P, "gap": U, "bad": U}
    assert verdicts(None, hist) == want_default
    assert verdicts(mod.PATCH, hist) == want_default
    # a patch recorded only in the first run: the hopped run is ABSENT alone, PRESENT through its ancestor
    hopped = {"hop": json.dumps(_synthetic(continued_from="r1")), "hop@r1": with_new}
    assert verdicts(new, hopped) == {"hop": P}
    assert verdicts(new, {"hop": json.dumps(_synthetic(continued_from="r1")), "hop@r1": clean}) == {"hop": A}
    # the report names the id that was scanned
    client = FakeClient([["new"]], {"new": with_new})
    r = asyncio.run(mod.scan(client.list_fn, client.fetch_fn, patch=new))
    assert f"patch={new} " in mod.render_text(r) and json.loads(mod.render_json(r, 3))["patch"] == new
    r = _scan(mod, client)
    assert f"patch={mod.PATCH} " in mod.render_text(r) and json.loads(mod.render_json(r, 0))["patch"] == mod.PATCH
    # the CLI: the option reaches the scan; without it the default id is scanned
    code, out = _cli_with_patch_id(mod, json.loads(with_new), ["--patch-id", new])
    assert code == 3 and out["patch"] == new and out["counts"]["MARKER_PRESENT"] == 1
    code, out = _cli_with_patch_id(mod, json.loads(with_new), [])
    assert code == 0 and out["patch"] == mod.PATCH and out["counts"]["MARKER_ABSENT"] == 1
    code, out = _cli_with_patch_id(mod, json.loads(clean), ["--patch-id", new])
    assert code == 0 and out["patch"] == new and out["counts"]["MARKER_ABSENT"] == 1
    code, out = _cli_with_patch_id(mod, gap, ["--patch-id", new])
    assert code == 2 and out["counts"]["UNKNOWN"] == 1
    # an empty id would match nothing and read as ABSENT: refused
    try:
        mod.main(["--address", "x:1", "--namespace", "n", "--patch-id", " "])
    except SystemExit as exc:
        assert exc.code == 2
    else:
        raise AssertionError("an empty --patch-id was accepted")



# ---------------------------------------------------------------------------
# Layer 1: the detector's defining assertions, runnable against any variant.
# ---------------------------------------------------------------------------
def detector_checks(mod: ModuleType) -> None:
    c, P, A, U = mod.classify, mod.PRESENT, mod.ABSENT, mod.UNKNOWN
    clean = json.dumps(_synthetic())
    # converged inputs
    assert c(POS)[0] == P
    assert c(clean)[0] == A
    assert c(json.dumps(_synthetic(marker=mod.PATCH)))[0] == P
    assert c(json.dumps(_synthetic(marker="other-patch")))[0] == A
    assert c(POS, patch="some-other-patch")[0] == A  # the id is really compared
    # never-ABSENT-on-uncertainty
    assert c(None)[0] == U
    assert c("{")[0] == U
    assert c('{"events": []}')[0] == U
    gap = _synthetic()
    del gap["events"][2]
    assert c(json.dumps(gap))[0] == U
    headless = _synthetic()
    headless["events"] = headless["events"][1:]
    assert c(json.dumps(headless))[0] == U
    wrong_first = _synthetic()
    wrong_first["events"][0]["eventType"] = "EVENT_TYPE_WORKFLOW_TASK_SCHEDULED"
    assert c(json.dumps(wrong_first))[0] == U
    bad = _synthetic(marker=mod.PATCH)
    bad["events"][-1]["markerRecordedEventAttributes"]["details"]["patch-data"]["payloads"][0]["data"] = "!!!x!!!"
    assert c(json.dumps(bad))[0] == U
    nopayload = _synthetic(marker=mod.PATCH)
    nopayload["events"][-1]["markerRecordedEventAttributes"]["details"] = {}
    assert c(json.dumps(nopayload))[0] == U
    # classify() judges ONE history: it reports continued_from, the chain verdict comes from scan()
    v = c(json.dumps(_synthetic(continued_from="prev-run")))
    assert v[0] == A and v.continued_from == "prev-run"
    assert c(clean).continued_from is None
    noid = _synthetic()
    del noid["events"][1]["eventId"]
    assert c(json.dumps(noid))[0] == U
    dup = _synthetic(n=4)
    for e, i in zip(dup["events"], [1, 2, 2, 4], strict=True):
        e["eventId"] = str(i)
    assert c(json.dumps(dup))[0] == U  # ids [1,2,2,4]: last id equals the count, still a gap
    _chain_checks(mod)
    _cli_checks(mod)
    _patch_id_checks(mod)
    # scan: fetch errors and bad bodies are UNKNOWN, never ABSENT
    pos = json.dumps(_synthetic(marker=mod.PATCH))
    cl = FakeClient([["ok", "boom", "bad", "pos"]], {"ok": clean, "boom": RuntimeError("rpc"), "bad": "{", "pos": pos})
    r = _scan(mod, cl)
    got = {e.workflow_id: e.verdict for e in r.entries}
    assert got == {"ok": A, "boom": U, "bad": U, "pos": P}
    assert r.listing_complete and r.exit_code() == 2
    # listing: a failed page after earlier successes is a failed scan
    cl = FakeClient([["a", "b"], RuntimeError("page 2 failed"), ["c"]], {k: clean for k in "abc"})
    r = _scan(mod, cl)
    assert not r.listing_complete and "page 2 failed" in (r.listing_error or "")
    assert r.exit_code() == 2 and r.exit_code(allow_present=True) == 2
    assert [e.workflow_id for e in r.entries] == ["a", "b"]
    # exit codes
    ok = _scan(mod, FakeClient([["a"]], {"a": clean}))
    assert ok.exit_code() == 0
    pr = _scan(mod, FakeClient([["a"]], {"a": POS}))
    assert pr.exit_code() == 3 and pr.exit_code(allow_present=True) == 0
    mixed = _scan(mod, FakeClient([["a", "b"]], {"a": POS, "b": "{"}))
    assert mixed.exit_code() == 2 and mixed.exit_code(allow_present=True) == 2
    assert _scan(mod, FakeClient([[]], {})).exit_code() == 0  # an honest empty listing
    _reason_checks(mod)
    _completeness_checks(mod)
    _connection_checks(mod)


class NotFound(Exception):
    status = type("Status", (), {"name": "NOT_FOUND"})()


class Unavailable(Exception):
    status = type("Status", (), {"name": "UNAVAILABLE"})()


def _reason_checks(mod: ModuleType) -> None:
    U = mod.UNKNOWN
    clean = lambda cont=None: json.dumps(_synthetic(continued_from=cont))  # noqa: E731
    # Retention gap: the server's absence signal on an ancestor still BLOCKS.
    e = _chain(mod, "r1", {"r1": clean("r2"), "r2": NotFound("gone")})
    assert e.verdict == U and e.reason == mod.R_RETENTION and "depth 2" in e.detail
    r = _scan(mod, FakeClient([["w"]], {"w": clean("r1"), "w@r1": NotFound("gone")}))
    assert r.exit_code() == 2 and r.exit_code(allow_present=True) == 2
    assert r.unknown_reasons() == {mod.R_RETENTION: 1}
    # Any other failure on an ancestor is not a retention gap.
    assert _chain(mod, "r1", {"r1": Unavailable("x")}).reason == mod.R_ANCESTOR
    assert _chain(mod, "r1", {"r1": RuntimeError("x")}).reason == mod.R_ANCESTOR
    assert _chain(mod, "r1", {"r1": "{"}).reason == mod.R_ANCESTOR
    # NOT_FOUND on a listed run itself is a failed fetch, not a retention gap of an ancestor.
    r = _scan(mod, FakeClient([["w"]], {"w": NotFound("gone")}))
    assert r.entries[0].verdict == U and r.entries[0].reason == mod.R_FETCH
    # Every other UNKNOWN path has its own code, and known verdicts have none.
    assert mod.classify(None).reason == mod.R_NO_HISTORY
    assert mod.classify("{").reason == mod.R_CORRUPT
    gap = _synthetic()
    del gap["events"][2]
    assert mod.classify(json.dumps(gap)).reason == mod.R_INCOMPLETE
    assert mod.classify(clean()).reason == ""
    # Non-list and non-dict shapes in a core_patch marker are UNKNOWN and do not raise.
    for odd in (5, True, "x", {"a": 1}):
        h = _synthetic(marker=mod.PATCH)
        h["events"][-1]["markerRecordedEventAttributes"]["details"]["patch-data"]["payloads"] = odd
        try:
            v = mod.classify(json.dumps(h))
        except TypeError as exc:
            raise AssertionError(f"classify raised on payloads={odd!r}") from exc
        assert v[0] == U and v.reason == mod.R_MARKER


def _completeness_checks(mod: ModuleType) -> None:
    clean = json.dumps(_synthetic())
    hist = {k: clean for k in "abc"}

    def run(counts: list[Any], max_workflows: int | None = None) -> Any:
        it = iter(counts)

        async def count_fn(query: str) -> int:
            v = next(it)
            if isinstance(v, Exception):
                raise v
            return v

        return asyncio.run(mod.scan(FakeClient([["a", "b", "c"]], hist).list_fn, FakeClient([], hist).fetch_fn,
                                    count_fn=count_fn, max_workflows=max_workflows))

    assert run([3, 3]).exit_code() == 0
    for bad in ([4, 4], [2, 2], [3, 4], [4, 3], [RuntimeError("no count")], [3, RuntimeError("no count")]):
        r = run(bad)
        assert not r.listing_complete and r.exit_code() == 2, bad
    r = run([3, 3], max_workflows=2)
    assert not r.listing_complete and r.exit_code() == 2 and len(r.entries) == 2
    assert run([3, 3], max_workflows=3).exit_code() == 0


def _connection_checks(mod: ModuleType) -> None:
    import argparse
    import contextlib
    import io
    import os

    def ns(**kw: Any) -> argparse.Namespace:
        base = {"tls": False, "tls_ca_file": None, "tls_cert_file": None, "tls_key_file": None,
                "tls_server_name": None, "api_key_env": None}
        return argparse.Namespace(**{**base, **kw})

    assert mod._connection_options(ns()) == {}
    assert mod._connection_options(ns(tls=True)) == {"tls": True}
    for bad in (ns(tls=True, api_key_env="SCAN_KEY_NOT_SET"), ns(tls_ca_file="x"),
                ns(tls=True, tls_cert_file="x"), ns(tls=True, tls_key_file="x"), ns(tls_server_name="x")):
        try:
            mod._connection_options(bad)
        except ValueError:
            continue
        raise AssertionError(f"bad connection flags accepted: {bad}")

    secret = "scan-secret-value"
    os.environ["SCAN_KEY"] = secret
    try:
        assert mod._connection_options(ns(tls=True, api_key_env="SCAN_KEY")) == {"tls": True, "api_key": secret}
        try:
            mod._connection_options(ns(api_key_env="SCAN_KEY"))
        except ValueError:
            pass
        else:
            raise AssertionError("an API key was accepted without TLS")
        seen: dict[str, Any] = {}

        async def leaky(address: str, namespace: str, **options: Any) -> Any:
            seen.update(options)
            raise ConnectionError(f"refused with {options.get('api_key')}")

        argv = ["--address", "x:1", "--namespace", "n", "--tls", "--api-key-env", "SCAN_KEY"]
        for fmt in ([], ["--json"]):
            seen.clear()
            saved, mod._connect = mod._connect, leaky
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    code = mod.main([*argv, *fmt])
            finally:
                mod._connect = saved
            assert seen["api_key"] == secret and code == 2
            assert secret not in buf.getvalue() and "[REDACTED]" in buf.getvalue()
    finally:
        del os.environ["SCAN_KEY"]


def _chain(mod: ModuleType, head_cont: str | None, ancestors: dict[str, Any]) -> Any:
    hist = {"w": json.dumps(_synthetic(continued_from=head_cont))}
    hist.update({f"w@{k}": v for k, v in ancestors.items()})
    return _scan(mod, FakeClient([["w"]], hist)).entries[0]


def _chain_checks(mod: ModuleType) -> None:
    A, P, U = mod.ABSENT, mod.PRESENT, mod.UNKNOWN
    clean = lambda cont=None: json.dumps(_synthetic(continued_from=cont))  # noqa: E731
    marked = json.dumps(_synthetic(marker=mod.PATCH))
    # whole chain clean -> ABSENT, still reporting continued_from
    e = _chain(mod, "r1", {"r1": clean("r2"), "r2": clean()})
    assert e.verdict == A and e.continued_from == "r1"
    # marker in the direct parent, and in a grandparent -> PRESENT
    assert _chain(mod, "r1", {"r1": marked}).verdict == P
    assert _chain(mod, "r1", {"r1": clean("r2"), "r2": marked}).verdict == P
    # ancestor fetch failure / empty / corrupt / gapped -> UNKNOWN
    assert _chain(mod, "r1", {"r1": RuntimeError("rpc")}).verdict == U
    assert _chain(mod, "r1", {"r1": clean("r2"), "r2": RuntimeError("rpc")}).verdict == U
    assert _chain(mod, "r1", {"r1": '{"events": []}'}).verdict == U
    assert _chain(mod, "r1", {"r1": "{"}).verdict == U
    # cycle -> UNKNOWN, reported as a loop
    e = _chain(mod, "r1", {"r1": clean("r2"), "r2": clean("r1")})
    assert e.verdict == U and "loops" in e.detail
    # chain deeper than the bound -> UNKNOWN; exactly at the bound is fine
    deep = {f"r{i}": clean(f"r{i + 1}") for i in range(1, 151)}
    deep["r151"] = clean()
    assert _chain(mod, "r1", deep).verdict == U
    ok = {f"r{i}": clean(f"r{i + 1}") for i in range(1, mod.MAX_CHAIN_DEPTH)}
    ok[f"r{mod.MAX_CHAIN_DEPTH}"] = clean()
    assert _chain(mod, "r1", ok).verdict == A
    # a continued run with an own-history problem is never rescued by clean ancestors
    bad_head = {"w": "{"}
    assert _scan(mod, FakeClient([["w"]], bad_head)).entries[0].verdict == U
    # exit code: an ABSENT-looking chain with an unreadable ancestor must block
    r = _scan(mod, FakeClient([["w"]], {"w": clean("r1"), "w@r1": RuntimeError("x")}))
    assert r.exit_code() == 2


def _cli_checks(mod: ModuleType) -> None:
    """main() must exit 2 on a connect failure and on a listing failure; 0 on a clean run."""
    import contextlib
    import io

    def run(connect: Any) -> tuple[int, str]:
        saved = mod._connect
        mod._connect = connect
        buf = io.StringIO()
        try:
            with contextlib.redirect_stdout(buf):
                code = mod.main(["--address", "x:1", "--namespace", "n", "--json"])
        finally:
            mod._connect = saved
        return code, buf.getvalue()

    async def refuse(address: str, namespace: str, **options: Any) -> Any:
        raise ConnectionError("refused")

    code, out = run(refuse)
    assert code == 2 and json.loads(out)["listing_complete"] is False

    class Hist:
        def to_json(self) -> str:
            return json.dumps(_synthetic())

    class Handle:
        async def fetch_history(self) -> Hist:
            return Hist()

    class Wf:
        id, run_id, workflow_type = "w", "r", "DevLoopWorkflow"

    def client(fail_after_first: bool) -> Any:
        class C:
            def list_workflows(self, query: str) -> AsyncIterator[Wf]:
                async def gen() -> AsyncIterator[Wf]:
                    yield Wf()
                    if fail_after_first:
                        raise RuntimeError("page 2 failed")
                return gen()

            def get_workflow_handle(self, wid: str, run_id: str | None = None) -> Handle:
                return Handle()

            async def count_workflows(self, query: str) -> Any:
                return type("Count", (), {"count": 1})()

        return C()

    async def failing_listing(address: str, namespace: str, **options: Any) -> Any:
        return client(True)

    async def fine(address: str, namespace: str, **options: Any) -> Any:
        return client(False)

    code, out = run(failing_listing)
    assert code == 2 and json.loads(out)["listing_complete"] is False
    code, out = run(fine)
    payload = json.loads(out)
    assert code == 0 and payload["counts"]["MARKER_ABSENT"] == 1
    # A retention lookup that fails is reported, and changes no verdict.
    assert payload["namespace"] == "n" and payload["retention_seconds"] is None and payload["retention_error"]


def test_real_tool_passes_detector_checks():
    detector_checks(ms)


# ---------------------------------------------------------------------------
# Layer 2a: ported offline classifier tests (MS-1) on the repo's real fixtures.
# ---------------------------------------------------------------------------
def test_positive_control_m1_history_is_detected():
    assert ms.classify(POS)[0] == ms.PRESENT


@pytest.mark.parametrize("name", LEGACY)
def test_negative_control_histories_are_legacy(name):
    assert ms.classify(_fx(name))[0] == ms.ABSENT


def test_the_detector_is_not_constant_mutating_the_marker_id_flips_the_verdict():
    assert ms.classify(POS, patch="some-other-patch")[0] == ms.ABSENT


def test_removing_the_marker_event_flips_present_to_unknown_or_absent_never_stays_present():
    d = json.loads(POS)
    drop = [e for e in d["events"] if _is_target_marker(e)]
    assert len(drop) == 1
    keep = [e for e in d["events"] if not _is_target_marker(e)]
    assert ms.classify(json.dumps({"events": keep}))[0] == ms.UNKNOWN
    for i, e in enumerate(keep, 1):
        e["eventId"] = str(i)
    assert ms.classify(json.dumps({"events": keep}))[0] == ms.ABSENT


@pytest.mark.parametrize(
    "raw", [None, "", "{", POS[: len(POS) // 2], "[]", '{"events": []}', '{"events": "x"}', "null"]
)
def test_unreadable_or_corrupt_is_unknown_not_absent(raw):
    assert ms.classify(raw)[0] == ms.UNKNOWN


def test_truncated_tail_is_absent_so_far_only_when_contiguous():
    d = json.loads(_fx("dev_loop_full.prepatch.json"))
    d["events"] = d["events"][:50]
    assert ms.classify(json.dumps(d))[0] == ms.ABSENT
    d["events"] = d["events"][5:]
    assert ms.classify(json.dumps(d))[0] == ms.UNKNOWN


def test_gap_in_event_ids_is_unknown():
    d = json.loads(_fx("dev_loop_full.prepatch.json"))
    del d["events"][10]
    assert ms.classify(json.dumps(d))[0] == ms.UNKNOWN


def test_undecodable_marker_payload_is_unknown():
    d = json.loads(POS)
    for e in d["events"]:
        m = e.get("markerRecordedEventAttributes")
        if m and m.get("markerName") == "core_patch":
            for p in m["details"]["patch-data"]["payloads"]:
                p["data"] = "!!!notbase64!!!"
    assert ms.classify(json.dumps(d))[0] == ms.UNKNOWN


# ---------------------------------------------------------------------------
# Layer 2b: scan(), listing/pagination fault injection, read-only guarantee, CLI output.
# ---------------------------------------------------------------------------
def test_listing_failure_on_first_page_is_blocking_with_no_entries():
    r = _scan(ms, FakeClient([RuntimeError("down")], {}))
    assert not r.listing_complete and r.entries == [] and r.exit_code() == 2


def test_listing_failure_after_successes_keeps_partial_entries_but_blocks():
    clean = json.dumps(_synthetic())
    r = _scan(ms, FakeClient([["a"], ["b"], ConnectionError("x")], {"a": clean, "b": clean}))
    assert [e.verdict for e in r.entries] == [ms.ABSENT, ms.ABSENT]
    assert r.exit_code() == 2  # two ABSENT entries must not satisfy "== 0"


def test_render_reports_counts_version_and_continued_from():
    hist = {"a": json.dumps(_synthetic(continued_from="r0")), "a@r0": json.dumps(_synthetic()), "b": "{"}
    r = _scan(ms, FakeClient([["a", "b"]], hist))
    text = ms.render_text(r)
    assert "scanner=MS-4" in text and "absent=1" in text and "unknown=1" in text and "continued_from=r0" in text
    payload = json.loads(ms.render_json(r, r.exit_code()))
    assert payload["scanner_version"] == "MS-4" and payload["exit_code"] == 2
    assert payload["counts"] == {"MARKER_PRESENT": 0, "MARKER_ABSENT": 1, "UNKNOWN": 1}
    assert payload["workflows"][0]["continued_from"] == "r0"


def test_adapters_use_only_read_methods():
    used: list[str] = []

    class Hist:
        def to_json(self) -> str:
            return json.dumps(_synthetic())

    class Handle:
        async def fetch_history(self) -> Hist:
            used.append("fetch_history")
            return Hist()

    class Wf:
        id, run_id, workflow_type = "w", "r", "DevLoopWorkflow"

    class Client:
        def list_workflows(self, query: str) -> AsyncIterator[Wf]:
            used.append("list_workflows")

            async def gen() -> AsyncIterator[Wf]:
                yield Wf()
            return gen()

        def get_workflow_handle(self, wid: str, run_id: str | None = None) -> Handle:
            used.append("get_workflow_handle")
            return Handle()

        def __getattr__(self, name: str) -> Any:  # signal/terminate/start_workflow/... must never be reached
            raise AssertionError(f"scanner touched non-read client method {name}")

    r = asyncio.run(ms.scan(*ms.temporal_adapters(Client())))
    assert [e.verdict for e in r.entries] == [ms.ABSENT]
    assert set(used) == {"list_workflows", "get_workflow_handle", "fetch_history"}


def test_source_has_no_mutating_temporal_calls():
    src = _TOOL.read_text()
    banned_calls = (".signal(", ".terminate(", ".cancel(", ".start_workflow(", ".execute_workflow(", ".execute_update(",
                    "execute_update_with_start_workflow", ".start_update(", "signal_with_start", "workflow_service.")
    # The one direct RPC the tool makes, read-only. Anything else fails below.
    src = src.replace("workflow_service.describe_namespace(", "")
    for banned in banned_calls:
        assert banned not in src


# ---------------------------------------------------------------------------
# Layer 2c: integration on REAL histories from the time-skipping test server.
# ---------------------------------------------------------------------------
@workflow.defn(name="MarkerPatched", sandboxed=False)
class Patched:
    @workflow.run
    async def run(self) -> bool:
        return workflow.patched(ms.PATCH)


@workflow.defn(name="MarkerOtherPatch", sandboxed=False)
class OtherPatch:
    @workflow.run
    async def run(self) -> bool:
        return workflow.patched("some-unrelated-patch")


@workflow.defn(name="MarkerFree", sandboxed=False)
class Plain:
    @workflow.run
    async def run(self, n: int) -> int:
        if n > 0:
            workflow.continue_as_new(n - 1)
        return n


@pytest.fixture(scope="module")
def real_scan_result() -> Any:
    from temporalio.testing import WorkflowEnvironment
    from temporalio.worker import Worker

    async def go() -> tuple[Any, dict[str, str], Any]:
        try:
            env = await WorkflowEnvironment.start_time_skipping()
        except Exception as e:  # noqa: BLE001 - test server binary unavailable offline
            pytest.skip(f"time-skipping test server unavailable: {e}")
        async with env:
            tq = f"tq-{uuid.uuid4()}"
            ids: dict[str, str] = {}
            listed: list[tuple[str, str, str]] = []
            async with Worker(env.client, task_queue=tq, workflows=[Patched, OtherPatch, Plain]):
                specs = (("patched", "MarkerPatched", Patched, []), ("other", "MarkerOtherPatch", OtherPatch, []),
                         ("plain", "MarkerFree", Plain, [1]))
                for key, name, wf, args in specs:
                    ids[key] = f"wf-{key}-{uuid.uuid4()}"
                    h = await env.client.start_workflow(wf.run, *args, id=ids[key], task_queue=tq)
                    await h.result(follow_runs=True)
                    listed.append((ids[key], h.first_execution_run_id or "", name))
            # The time-skipping server does not implement visibility listing, so the
            # list side is a fixed list of the real runs; fetching is the real client path.
            first_hist = json.loads(await _fetch(env.client, *listed[2][:2]))
            new_run = first_hist["events"][-1]["workflowExecutionContinuedAsNewEventAttributes"]["newExecutionRunId"]
            listed.append((ids["plain"], new_run, "MarkerFree"))

            async def list_fn(query: str) -> AsyncIterator[tuple[str, str, str]]:
                for item in listed:
                    yield item

            _, fetch_fn = ms.temporal_adapters(env.client)
            result = await ms.scan(list_fn, fetch_fn)
            real_list_fn, _ = ms.temporal_adapters(env.client)
            list_failure = await ms.scan(real_list_fn, fetch_fn)
            return result, ids, list_failure

    return asyncio.run(go())


async def _fetch(client: Any, wid: str, run_id: str) -> str:
    return await ms.temporal_adapters(client)[1](wid, run_id)


def test_integration_real_histories(real_scan_result):
    result, ids, _ = real_scan_result
    by_id = {e.workflow_id: e for e in result.entries}
    assert result.listing_complete
    assert by_id[ids["patched"]].verdict == ms.PRESENT
    assert by_id[ids["other"]].verdict == ms.ABSENT
    plain_entries = [e for e in result.entries if e.workflow_type == "MarkerFree"]
    assert len(plain_entries) == 2  # original run + its continue-as-new successor, same workflow id
    assert all(e.verdict == ms.ABSENT for e in plain_entries)
    assert sorted(e.continued_from is not None for e in plain_entries) == [False, True]
    assert result.exit_code() == 3 and result.exit_code(allow_present=True) == 0


def test_integration_unlistable_server_is_a_failed_scan_not_an_empty_one(real_scan_result):
    # The time-skipping server answers ListWorkflowExecutions with UNIMPLEMENTED:
    # that must surface as a blocking failed listing, never as "no workflows".
    _, _, failure = real_scan_result
    assert not failure.listing_complete and failure.exit_code() == 2


# ---------------------------------------------------------------------------
# Layer 3: mutation tests. Each mutant must make detector_checks FAIL.
# (snippet, replacement, replace_all). The snippet must occur in the tool source.
# ---------------------------------------------------------------------------
MUTATIONS: dict[str, tuple[str, str]] = {
    "fetch-error-becomes-absent": ('v = Verdict(UNKNOWN, f"fetch failed', 'v = Verdict(ABSENT, f"fetch failed'),
    "ignore-id-gaps": ("if ids != list(range(1, len(ids) + 1)):", "if False:"),
    "ignore-failed-listing-page": ("return ScanResult(entries, False, f", "return ScanResult(entries, True, f"),
    "swallow-decode-errors": ("undecodable += 1", "pass"),
    "skip-started-event-check": ('if ids[0] != 1 or ev[0].get("eventType") != _STARTED:', "if False:"),
    "unknown-does-not-block": ("if not self.listing_complete or c[UNKNOWN]:", "if not self.listing_complete:"),
    "listing-incomplete-does-not-block": ("if not self.listing_complete or c[UNKNOWN]:", "if c[UNKNOWN]:"),
    "unparseable-becomes-absent": ('return Verdict(UNKNOWN, f"unparseable', 'return Verdict(ABSENT, f"unparseable'),
    "missing-history-becomes-absent": ('return Verdict(UNKNOWN, "no history"', 'return Verdict(ABSENT, "no history"'),
    "empty-history-becomes-absent": ('return Verdict(UNKNOWN, "no events"', 'return Verdict(ABSENT, "no events"'),
    "patch-id-option-removed": (
        'p.add_argument("--patch-id", default=PATCH, help=f"patch id to look for (default: {PATCH})")',
        "p.set_defaults(patch_id=PATCH)",
    ),
    "patch-id-option-not-passed-to-scan": ("            patch=args.patch_id,\n", ""),
    "patch-id-not-in-report": ('"patch": result.patch,', '"patch": PATCH,'),
    "patch-id-not-compared": ('["id"] == patch:', '["id"] is not None:'),
    "drop-continued-from": ('return attrs.get("continuedExecutionRunId") or None', "return None"),
    "event-without-id-becomes-absent": ('return Verdict(UNKNOWN, "event without eventId", cont',
                                        'return Verdict(ABSENT, "event without eventId", cont'),
    "weak-gap-check": ("if ids != list(range(1, len(ids) + 1)):", "if ids[-1] != len(ids):"),
    "connect-failure-marked-complete": ('ScanResult([], False, f"connect failed',
                                        'ScanResult([], True, f"connect failed'),
    "chain-not-walked": ("if own.verdict != ABSENT or not own.continued_from:", "if True:"),
    "ancestor-marker-ignored": ("if anc.verdict == PRESENT:", "if False:"),
    "ancestor-fetch-failure-becomes-absent": ('{exc}"\n            return Verdict(UNKNOWN,',
                                              '{exc}"\n            return Verdict(ABSENT,'),
    "unknown-ancestor-ignored": ("if anc.verdict != ABSENT:", "if False:"),
    "chain-stops-after-parent": ("prev = anc.continued_from", "prev = None"),
    "chain-depth-unbounded": ("if depth >= MAX_CHAIN_DEPTH:", "if False:"),
    "chain-cycle-undetected": ("if prev in seen:", "if False:"),
    "present-allowed-never-informational": ("if c[PRESENT] and not allow_present:", "if False:"),
    "retention-gap-becomes-absent": ('return Verdict(UNKNOWN, detail, own.continued_from, R_RETENTION)',
                                     'return Verdict(ABSENT, detail, own.continued_from, R_RETENTION)'),
    "retention-gap-not-told-apart": ("if _not_found(exc):", "if False:"),
    "any-failure-is-a-retention-gap": ('== "NOT_FOUND"', '!= "NOPE"'),
    "non-list-payload-not-checked": (" or not isinstance(payloads, list)", ""),
    "count-mismatch-ignored": ("if not expected == after == len(entries):", "if False:"),
    "count-after-not-compared": ("if not expected == after == len(entries):", "if not expected == len(entries):"),
    "count-before-not-compared": ("if not expected == after == len(entries):", "if not after == len(entries):"),
    "workflow-limit-ignored": ("if max_workflows is not None and len(entries) >= max_workflows:", "if False:"),
    "api-key-allowed-in-plaintext": ("if not args.tls:\n            raise ValueError(\"--api",
                                     "if False:\n            raise ValueError(\"--api"),
    "empty-api-key-accepted": ("if not key:", "if False:"),
    "api-key-not-scrubbed": ('return text.replace(secret, "[REDACTED]") if secret else text', "return text"),
}


def _mutant(tmp_path: Path, name: str) -> ModuleType:
    old, new = MUTATIONS[name]
    src = _TOOL.read_text()
    assert old in src, f"mutation {name!r}: snippet vanished from the tool; update the mutation"
    path = tmp_path / f"mutant_{name.replace('-', '_')}.py"
    path.write_text(src.replace(old, new))
    return _load(path, f"mutant_{name.replace('-', '_')}")


@pytest.mark.parametrize("name", sorted(MUTATIONS))
def test_mutation_is_killed(tmp_path, name):
    mutant = _mutant(tmp_path, name)
    try:
        detector_checks(mutant)
    except AssertionError:
        return  # killed by a detector assertion
    except Exception as exc:  # noqa: BLE001 - a crash is not a kill: it proves nothing about the assertions
        pytest.fail(f"mutant {name!r} crashed with {type(exc).__name__}: {exc}; that is not a detector kill")
    pytest.fail(f"mutant {name!r} survived the detector assertions")


def test_mutation_harness_is_green_on_an_unmutated_copy(tmp_path):
    path = tmp_path / "copy_of_tool.py"
    path.write_text(_TOOL.read_text())
    detector_checks(_load(path, "copy_of_tool"))
