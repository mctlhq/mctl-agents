"""Guard for the #918 finding-oracle spike report, corpus, evidence and harness."""
from __future__ import annotations

import copy
import json
import re
import socket
from pathlib import Path

import pytest

from tools import spike_918_finding_oracles as h

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "docs" / "spikes" / "918-finding-oracle-classes.md"
SPIKE = ROOT / "docs" / "spikes" / "918"
STATUSES = {"DRAFT", "NO_GENERIC_GATE", "VALIDATE_SELECTED_FINDING_CLASSES", "ADD_VALIDATED_FINDING_CONTRACT"}
RATINGS = {"DETERMINISTIC", "BOUNDED_BUT_NONDETERMINISTIC", "NO_SAFE_ORACLE"}
CLASSES = {
    "runtime/test failure",
    "static invariant / schema",
    "security with safe local oracle",
    "dependency / version affectedness",
    "stale factual premise",
    "architecture / design",
    "governance / ownership",
    "incomplete requirement / ambiguous decision",
}
TOKEN_RE = r"ghp_|gho_|github_pat_|sk-ant-|-----BEGIN"


def _text() -> str:
    return REPORT.read_text(encoding="utf-8")


def _table(heading: str) -> list[list[str]]:
    section = _text().split(heading, 1)[1].split("\n## ", 1)[0].split("\n### ", 1)[0]
    rows = []
    for line in section.splitlines():
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if not set(cells[0]) <= {"-"}:
                rows.append(cells)
    return rows[1:]


def test_status_line() -> None:
    lines = [ln for ln in _text().splitlines() if ln.startswith("STATUS:")]
    assert len(lines) == 1
    assert lines[0].split(":", 1)[1].strip() in STATUSES


def test_taxonomy() -> None:
    rows = _table("## Taxonomy and oracle map")
    assert {r[0] for r in rows} == CLASSES
    for r in rows:
        assert len(r) == 7, r
        assert r[1] in RATINGS, r
        assert r[2], r


def test_corpus_and_results() -> None:
    corpus, evidence = h.load()
    assert len(corpus) <= 60
    for f in corpus:
        assert f["ground_truth"] in {"true_positive", "noise", "unlabelled"}
        assert f["label_basis"]
        assert len(f["claim"]) <= 300
    results = json.loads((SPIKE / "results.json").read_text(encoding="utf-8"))
    assert results["metrics"] == h.metrics(corpus, [h.evaluate(f, evidence) for f in corpus])
    ids = {f["id"]: f for f in corpus}
    for r in results["results"]:
        assert r["outcome"] in h.OUTCOMES
        if r["outcome"] == "FALSE":
            assert ids[r["id"]]["fields"]["check"] in h.FALSITY_PROVABLE_CHECKS
        if r["outcome"] == "VALIDATED_FINDING":
            assert r["binding"]


def test_false_has_completeness_note() -> None:
    rows = {r[0]: r for r in _table("## Taxonomy and oracle map")}
    assert rows["stale factual premise"][5].startswith("yes: ")


def test_independent_of_claim_text() -> None:
    corpus, evidence = h.load()
    for f in corpus:
        g = copy.deepcopy(f)
        g["claim"] = "completely different text"
        a, b = h.evaluate(f, evidence), h.evaluate(g, evidence)
        a.pop("latency_ms"), b.pop("latency_ms")
        assert a == b


def test_failed_evidence_is_unknown_and_no_oracle_is_hypothesis() -> None:
    f = {"id": "x", "class": "static_invariant", "fields": {"check": "file_symbol", "evidence": "e",
         "path": "p", "expect": "present"}}
    for ev in ({"e": {"status": "failed", "data": {}}}, {"e": {"status": "partial", "data": {}}}, {}):
        assert h.evaluate(f, ev)["outcome"] == "UNKNOWN"
    g = {"id": "y", "class": "architecture_design", "fields": {"check": "none"}}
    assert h.evaluate(g, {})["outcome"] == "HYPOTHESIS"


def test_no_authority_fields_and_no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> None:
        raise AssertionError("network used")

    monkeypatch.setattr(socket, "socket", boom)
    corpus, evidence = h.load()
    for f in corpus:
        for key in h.evaluate(f, evidence):
            assert not re.search(r"approve|allow|grant|permit|authorized", key)


def test_metrics_table_complete() -> None:
    text = _text().split("### Metrics", 1)[1].split("\n## ", 1)[0]
    for word in ("precision", "false-positive", "coverage", "false negatives", "NOT_REPRODUCED",
                 "UNKNOWN", "HYPOTHESIS", "latency", "model cost", "time-to-decision"):
        assert word in text
    assert "not-measured:" in text


def test_no_token_shaped_strings() -> None:
    for p in [REPORT, *SPIKE.rglob("*")]:
        if p.is_file():
            assert not re.search(TOKEN_RE, p.read_text(encoding="utf-8")), p
