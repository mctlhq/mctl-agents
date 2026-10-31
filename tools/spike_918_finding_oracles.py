"""Offline oracle harness for spike #918.

Validators read structured claim fields plus captured evidence, never the
claim's free text. No network, no exploit execution, and no output field
that grants anything. Replays deterministically over the committed corpus.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import yaml

OUTCOMES = ("VALIDATED_FINDING", "NOT_REPRODUCED", "FALSE", "UNKNOWN", "HYPOTHESIS")
# Classes with a deterministic or bounded oracle in this harness.
ELIGIBLE = {"runtime_test_failure", "static_invariant", "dependency_version", "stale_premise"}
# Only classes where a complete oracle proves falsity (canonical state field).
FALSITY_PROVABLE_CHECKS = {"github_state"}
ROOT = Path(__file__).resolve().parent.parent
SPIKE = ROOT / "docs" / "spikes" / "918"


def _version(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".") if p.isdigit())


def _binding(ev: dict[str, Any]) -> str:
    return f"{ev.get('revision')}@{ev.get('captured_at')}"


def _validate(fields: dict[str, Any], ev: dict[str, Any]) -> str:
    check = fields["check"]
    data = ev["data"]
    if check == "github_state":
        return "VALIDATED_FINDING" if data.get("state") == fields["expect_state"] else "FALSE"
    if check == "file_symbol":
        syms = data["files"].get(fields["path"])
        present = syms is not None and (fields.get("symbol") is None or fields["symbol"] in syms)
        return "VALIDATED_FINDING" if present == (fields["expect"] == "present") else "NOT_REPRODUCED"
    if check == "lockfile":
        ver = data["versions"].get(fields["package"])
        if ver is None:
            return "NOT_REPRODUCED"
        hit = _version(ver) < _version(fields["affected_below"])
        return "VALIDATED_FINDING" if hit else "NOT_REPRODUCED"
    if check == "test_rerun":
        runs = data["reruns"]
        if len(set(runs)) != 1:
            return "UNKNOWN"  # flaky: same SHA, disagreeing reruns
        return "VALIDATED_FINDING" if runs[0] == fields["expect"] else "NOT_REPRODUCED"
    return "HYPOTHESIS"


def evaluate(finding: dict[str, Any], evidence: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Return one result record. Reads only structured fields and evidence."""
    start = time.perf_counter()
    fields = finding["fields"]
    cls = finding["class"]
    outcome = "HYPOTHESIS"
    binding: str | None = None
    if cls in ELIGIBLE and fields.get("check") not in (None, "none"):
        ev = evidence.get(fields["evidence"])
        if ev is None or ev.get("status") != "ok":
            outcome = "UNKNOWN"
            binding = _binding(ev) if ev else None
        else:
            try:
                outcome = _validate(fields, ev)
            except (KeyError, TypeError, ValueError):
                outcome = "UNKNOWN"
            binding = _binding(ev)
            if outcome == "FALSE" and fields["check"] not in FALSITY_PROVABLE_CHECKS:
                outcome = "NOT_REPRODUCED"
    return {
        "id": finding["id"],
        "class": cls,
        "outcome": outcome,
        "binding": binding,
        "latency_ms": round((time.perf_counter() - start) * 1000, 3),
    }


def load() -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    corpus = yaml.safe_load((SPIKE / "corpus.yaml").read_text(encoding="utf-8"))["findings"]
    evidence = json.loads((SPIKE / "evidence" / "captures.json").read_text(encoding="utf-8"))
    return corpus, evidence


def metrics(corpus: list[dict[str, Any]], results: list[dict[str, Any]]) -> dict[str, Any]:
    """Before/after comparison. Before: every finding actionable. After: VALIDATED only."""
    by_id = {r["id"]: r for r in results}
    elig = [f for f in corpus if f["class"] in ELIGIBLE]
    lab = [f for f in elig if f["ground_truth"] != "unlabelled"]
    tp = [f for f in lab if f["ground_truth"] == "true_positive"]
    noise = [f for f in lab if f["ground_truth"] == "noise"]
    val = [f for f in lab if by_id[f["id"]]["outcome"] == "VALIDATED_FINDING"]
    val_tp = [f for f in val if f["ground_truth"] == "true_positive"]
    val_noise = [f for f in val if f["ground_truth"] == "noise"]

    def rate(n: int, d: int) -> float | str:
        return round(n / d, 3) if d else "not-measured: empty denominator"

    def count(name: str, group: list[dict[str, Any]]) -> int:
        return sum(1 for f in group if by_id[f["id"]]["outcome"] == name)

    return {
        "corpus_size": len(corpus),
        "eligible": len(elig),
        "labelled_eligible": len(lab),
        "precision_before": rate(len(tp), len(lab)),
        "precision_after": rate(len(val_tp), len(val)),
        "fpr_before": rate(len(noise), len(noise)),
        "fpr_after": rate(len(val_noise), len(noise)),
        "coverage": rate(len(elig), len(corpus)),
        "false_negatives_from_gating": sum(
            1 for f in tp if by_id[f["id"]]["outcome"] in ("NOT_REPRODUCED", "UNKNOWN")
        ),
        "not_reproduced_rate": rate(count("NOT_REPRODUCED", elig), len(elig)),
        "unknown_rate": rate(count("UNKNOWN", elig), len(elig)),
        "false_rate": rate(count("FALSE", elig), len(elig)),
        "hypothesis_by_design": sum(1 for f in corpus if f["class"] not in ELIGIBLE),
        "model_cost_usd": 0,
        "reviewer_time_to_decision": "not-measured: no human study; timestamps not captured",
    }


def main() -> int:
    corpus, evidence = load()
    results = [evaluate(f, evidence) for f in corpus]
    if "--results" in sys.argv:
        stable = [{k: v for k, v in r.items() if k != "latency_ms"} for r in results]
        out = {"metrics": metrics(corpus, results), "results": stable}
        (SPIKE / "results.json").write_text(json.dumps(out, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return 0
    for r in results:
        print(json.dumps(r, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
