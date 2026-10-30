"""Parse fixture JSON lines into per-runtime JSON files and markdown tables.

Usage: python -I collect.py --out DIR --runs N runc=runc.log gvisor=gvisor.log [...]
Each log holds the JSON lines of all N passes of one runtime (other lines are
ignored): fixture ops ({"op", "ok", "seconds", "stderr_tail"}), probes
({"probe", "value"}) and metrics ({"metric", ...}). Every record is kept, so a
failure in any pass survives later passes.

Scorecard cells follow the spike #881 report grammar: pass (all N passes ok),
fail:<k>/<N> <stderr tail> (k passes failed or produced no record), or
not-exercised (no record at all). A missing record is a failure, never a pass:
a pass that crashed before reporting must not shrink the denominator.
"""

from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def parse_lines(text: str) -> dict[str, list[dict]]:
    """Group every record by key: op name, "probe:<name>" or "metric:<name>"."""
    out: dict[str, list[dict]] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(rec, dict):
            continue
        if "op" in rec and "ok" in rec:
            key = str(rec["op"])
        elif "probe" in rec:
            key = f"probe:{rec['probe']}"
        elif "metric" in rec:
            key = f"metric:{rec['metric']}"
        else:
            continue
        out.setdefault(key, []).append(rec)
    return out


def _ops(records: dict[str, list[dict]]) -> dict[str, list[dict]]:
    return {k: v for k, v in records.items() if ":" not in k}


def cell(recs: list[dict] | None, runs: int) -> str:
    if not recs:
        return "not-exercised"
    failed = [r for r in recs if not r.get("ok")]
    missing = max(runs - len(recs), 0)
    if not failed and not missing:
        return "pass"
    tail = ""
    if failed:
        tail = " ".join(str(failed[-1].get("stderr_tail", "")).split()).replace("|", "/")
    elif missing:
        tail = "no record from the pass"
    return f"fail:{len(failed) + missing}/{max(runs, len(recs))} {tail or 'no stderr'}"


def median_seconds(recs: list[dict] | None) -> float | None:
    vals = [float(r["seconds"]) for r in recs or [] if r.get("ok") and "seconds" in r]
    return statistics.median(vals) if vals else None


def markdown(results: dict[str, dict[str, list[dict]]], runs: int) -> str:
    runtimes = list(results)
    ops = sorted({op for r in results.values() for op in _ops(r)})
    lines = ["| op | " + " | ".join(runtimes) + " |", "|---|" + "---|" * len(runtimes)]
    for op in ops:
        lines.append(f"| {op} | " + " | ".join(cell(results[r].get(op), runs) for r in runtimes) + " |")
    return "\n".join(lines) + "\n"


def performance(results: dict[str, dict[str, list[dict]]], base: str = "runc") -> str:
    """Median seconds of passing records per op, plus overhead versus base."""
    runtimes = list(results)
    ops = sorted({op for r in results.values() for op in _ops(r)})
    head = ["op"] + [f"{r} median s" for r in runtimes] + [f"{r} vs {base}" for r in runtimes if r != base]
    lines = ["| " + " | ".join(head) + " |", "|---|" + "---|" * (len(head) - 1)]
    for op in ops:
        meds = {r: median_seconds(results[r].get(op)) for r in runtimes}
        row = [op] + ["n/a" if meds[r] is None else f"{meds[r]:.3f}" for r in runtimes]
        for r in runtimes:
            if r == base:
                continue
            b, v = meds.get(base), meds[r]
            row.append("n/a" if not b or v is None else f"{(v - b) / b * 100:+.1f}%")
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines) + "\n"


def _percentile(vals: list[float], q: int) -> float:
    """Nearest-rank percentile, so a p95 of 10 samples is an observed value."""
    s = sorted(vals)
    rank = -(-len(s) * q // 100)  # ceil(n * q / 100)
    return s[max(0, min(len(s) - 1, rank - 1))]


def _stat(text: str, key: str) -> float | None:
    """Sum of `key=value` (io.stat) or `key value` (cpu.stat) fields in a cgroup file dump."""
    toks = str(text).replace("=", " ").split()
    vals = [float(toks[i + 1]) for i, t in enumerate(toks[:-1]) if t == key and toks[i + 1].isdigit()]
    return sum(vals) if vals else None


def resources(results: dict[str, dict[str, list[dict]]]) -> str:
    """Startup p50/p95 and median cgroup CPU, peak memory and I/O per runtime.

    Reported next to the wall-clock gate, never instead of it. A value the
    runtime did not expose is n/a, not zero.
    """
    head = ["runtime", "startup p50 s", "startup p95 s", "cpu usage s", "memory peak MiB", "io bytes MiB"]
    lines = ["| " + " | ".join(head) + " |", "|---|" + "---|" * (len(head) - 1)]

    def med(vals: list[float], scale: float) -> str:
        return f"{statistics.median(vals) / scale:.1f}" if vals else "n/a"

    for name, recs in results.items():
        starts = [float(r["seconds"]) for r in recs.get("metric:startup", []) if "seconds" in r]
        cg = recs.get("metric:cgroup", [])
        cpu = [v for r in cg if (v := _stat(r.get("cpu_stat", ""), "usage_usec")) is not None]
        mem = [float(m) for r in cg if (m := str(r.get("memory_peak", "")).strip()).isdigit()]
        io = []
        for r in cg:
            rb, wb = _stat(r.get("io_stat", ""), "rbytes"), _stat(r.get("io_stat", ""), "wbytes")
            if rb is not None or wb is not None:
                io.append((rb or 0) + (wb or 0))
        lines.append(
            "| "
            + " | ".join(
                [
                    name,
                    f"{_percentile(starts, 50):.1f}" if starts else "n/a",
                    f"{_percentile(starts, 95):.1f}" if starts else "n/a",
                    med(cpu, 1e6),
                    med(mem, 2**20),
                    med(io, 2**20),
                ]
            )
            + " |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--runs", type=int, required=True, help="passes dispatched per runtime")
    ap.add_argument("logs", nargs="+", help="runtime=path")
    args = ap.parse_args()
    results: dict[str, dict[str, list[dict]]] = {}
    for spec in args.logs:
        name, _, path = spec.partition("=")
        p = Path(path)
        # A runtime whose log was never written is reported as not-exercised.
        text = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        results[name] = parse_lines(text)
    args.out.mkdir(parents=True, exist_ok=True)
    for name, recs in results.items():
        (args.out / f"{name}.json").write_text(json.dumps(recs, indent=2, sort_keys=True), encoding="utf-8")
    score = markdown(results, args.runs)
    perf = performance(results) + "\n" + resources(results)
    (args.out / "scorecard.md").write_text(score, encoding="utf-8")
    (args.out / "performance.md").write_text(perf, encoding="utf-8")
    print(score)
    print(perf)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
