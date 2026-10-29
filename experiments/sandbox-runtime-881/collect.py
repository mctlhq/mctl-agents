"""Parse fixture JSON lines into a per-runtime JSON file and a markdown table.

Usage: python -I collect.py --out DIR runc=runc.log gvisor=gvisor.log [...]
Each log holds the fixture's JSON lines (other lines are ignored). Repeated
runs of one op use the last record. Cells are pass, fail:<stderr tail> or
not-exercised, matching the spike #881 report's scorecard grammar.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def parse_lines(text: str) -> dict[str, dict]:
    ops: dict[str, dict] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "op" in rec and "ok" in rec:
            ops[str(rec["op"])] = rec
    return ops


def cell(rec: dict | None) -> str:
    if rec is None:
        return "not-exercised"
    if rec["ok"]:
        return "pass"
    tail = " ".join(str(rec.get("stderr_tail", "")).split()).replace("|", "/")
    return f"fail:{tail or 'no stderr'}"


def markdown(results: dict[str, dict[str, dict]]) -> str:
    runtimes = list(results)
    ops = sorted({op for r in results.values() for op in r})
    lines = ["| op | " + " | ".join(runtimes) + " |", "|---|" + "---|" * len(runtimes)]
    for op in ops:
        lines.append(f"| {op} | " + " | ".join(cell(results[r].get(op)) for r in runtimes) + " |")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("logs", nargs="+", help="runtime=path")
    args = ap.parse_args()
    results: dict[str, dict[str, dict]] = {}
    for spec in args.logs:
        name, _, path = spec.partition("=")
        results[name] = parse_lines(Path(path).read_text(encoding="utf-8"))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, ops in results.items():
        (args.out / f"{name}.json").write_text(json.dumps(ops, indent=2, sort_keys=True), encoding="utf-8")
    (args.out / "scorecard.md").write_text(markdown(results), encoding="utf-8")
    print(markdown(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
