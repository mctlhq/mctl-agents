"""Read-only capacity analysis for spike #917 (verified Claude Ultra).

Reads a JSONL of per-run records plus a run manifest (schema in
docs/spikes/917/records-schema.md) and prints per-window throughput,
429 rejections by rate_limit_type, weekly consumption series and marginal
throughput. Nothing imports this module; it never prints a provider-billed
figure. Run with: python -I tools/spike_917_capacity.py RECORDS MANIFEST
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

NOT_OBSERVED = "not observed"
COST_LABEL = "calculated_cost (list-price estimate, pricing_version={v})"


def load_records(path: Path) -> list[dict[str, Any]] | None:
    if not path.is_file():
        return None
    out: list[dict[str, Any]] = []
    for line in path.read_text().splitlines():
        if line.strip():
            out.append(json.loads(line))
    return out


def load_manifest(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else None


def _hours(window: dict[str, Any]) -> float | None:
    start, end = window.get("start"), window.get("end")
    if start is None or end is None or end <= start:
        return None
    return float(end - start) / 3600.0


def _rate(count: int, hours: float | None, per: float) -> str:
    if hours is None:
        return NOT_OBSERVED
    return f"{count / hours * per:.2f}"


def rejection_summary(records: list[dict[str, Any]]) -> dict[str, tuple[int, float]]:
    """Count and lost wall-clock seconds of rejected events per type."""
    acc: dict[str, list[float]] = defaultdict(lambda: [0, 0.0])
    for rec in records:
        for ev in rec.get("rate_limit_events") or []:
            if ev.get("status") != "rejected":
                continue
            kind = ev.get("rate_limit_type") or "unknown"
            acc[kind][0] += 1
            acc[kind][1] += float(ev.get("lost_s") or 0.0)
    return {k: (int(v[0]), v[1]) for k, v in acc.items()}


def stage_rates(records: list[dict[str, Any]], hours: float | None) -> dict[tuple[str, str], dict[str, Any]]:
    """Per (stage, model): completed stages, output tokens, cost estimate."""
    acc: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        if rec.get("outcome") != "completed":
            continue
        key = (rec.get("stage") or "unknown", rec.get("model") or "unknown")
        row = acc.setdefault(key, {"n": 0, "out": 0, "cost": None, "pv": set()})
        row["n"] += 1
        row["out"] += int(rec.get("output_tokens") or 0)
        if rec.get("calculated_cost_usd") is not None:
            row["cost"] = (row["cost"] or 0.0) + float(rec["calculated_cost_usd"])
            row["pv"].add(str(rec.get("pricing_version") or "unknown"))
    for row in acc.values():
        row["per_hour"] = _rate(row["n"], hours, 1)
        row["per_day"] = _rate(row["n"], hours, 24)
        row["out_per_hour"] = _rate(row["out"], hours, 1)
    return acc


def weekly_series(manifest: dict[str, Any]) -> dict[str, list[tuple[float, float]]]:
    series: dict[str, list[tuple[float, float]]] = {"all_models": [], "fable": []}
    for s in manifest.get("weekly_samples") or []:
        for name, field in (
            ("all_models", "all_models_remaining_pct"),
            ("fable", "fable_remaining_pct"),
        ):
            if s.get(field) is not None:
                series[name].append((float(s["ts"]), float(s[field])))
    return series


def binding_limit(series: dict[str, list[tuple[float, float]]]) -> str:
    """Name the limit with the lowest latest remaining %, or not observed."""
    latest = {k: sorted(v)[-1][1] for k, v in series.items() if v}
    if not latest:
        return NOT_OBSERVED
    return min(latest, key=lambda k: latest[k])


def marginal_throughput(records: list[dict[str, Any]], manifest: dict[str, Any]) -> str:
    """Completed stages between first would-be five_hour boundary and first weekly warning."""
    t0 = manifest.get("first_five_hour_boundary_ts")
    t1 = None
    for rec in records:
        for ev in rec.get("rate_limit_events") or []:
            if ev.get("status") == "allowed_warning" and ev.get("rate_limit_type") == "seven_day":
                ts = ev.get("ts")
                if ts is not None and (t1 is None or ts < t1):
                    t1 = ts
    if t0 is None or t1 is None or t1 <= t0:
        return NOT_OBSERVED
    n = sum(1 for r in records if r.get("outcome") == "completed" and t0 <= r.get("ended_at", -1) <= t1)
    return f"{n} stages in {(t1 - t0) / 3600.0:.2f}h ({n / ((t1 - t0) / 3600.0):.2f}/h)"


def report(records: list[dict[str, Any]] | None, manifest: dict[str, Any] | None) -> str:
    lines: list[str] = []
    if records is None or manifest is None:
        lines.append(f"records: {NOT_OBSERVED if records is None else 'ok'}")
        lines.append(f"manifest: {NOT_OBSERVED if manifest is None else 'ok'}")
        return "\n".join(lines)
    for name in sorted(manifest.get("sources") or {}):
        src = manifest["sources"][name]
        state = "observed" if src.get("observed") else f"{NOT_OBSERVED} ({src.get('reason', 'no reason given')})"
        lines.append(f"source {name}: {state}")
    for wname, window in sorted((manifest.get("windows") or {}).items()):
        recs = [r for r in records if r.get("window") == wname]
        hours = _hours(window)
        lines.append(f"== window {wname} ==")
        lines.append(f"hours: {NOT_OBSERVED if hours is None else f'{hours:.2f}'}")
        for (stage, model), row in sorted(stage_rates(recs, hours).items()):
            cost = (
                NOT_OBSERVED
                if row["cost"] is None
                else f"{row['cost']:.2f} " + COST_LABEL.format(v=",".join(sorted(row["pv"])))
            )
            lines.append(
                f"stage={stage} model={model} completed={row['n']} "
                f"per_hour={row['per_hour']} per_day={row['per_day']} "
                f"output_tokens_per_hour={row['out_per_hour']} cost={cost}"
            )
        rej = rejection_summary(recs)
        if not rej:
            lines.append("rejections: none recorded")
        for kind, (n, lost) in sorted(rej.items()):
            lines.append(f"rejections rate_limit_type={kind} count={n} lost_wall_clock_s={lost:.0f}")
    series = weekly_series(manifest)
    for name in ("all_models", "fable"):
        pts = series[name]
        text = NOT_OBSERVED if not pts else ", ".join(f"{int(t)}:{p:.0f}%" for t, p in sorted(pts))
        lines.append(f"weekly remaining {name}: {text}")
    lines.append(f"binding weekly limit: {binding_limit(series)}")
    after = [r for r in records if r.get("window") == "after"]
    lines.append(f"marginal throughput: {marginal_throughput(after, manifest)}")
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: spike_917_capacity.py RECORDS.jsonl MANIFEST.json", file=sys.stderr)
        return 2
    print(report(load_records(Path(argv[1])), load_manifest(Path(argv[2]))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
