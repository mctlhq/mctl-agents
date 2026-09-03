"""Validate (and optionally render) docs/diagrams/archify/*.json with archify.

One diagram per <name>.<type>.json; the type is read from the file name and
cross-checked against the JSON's own diagram_type so a misnamed file cannot be
validated as the wrong kind and pass by accident.

Exit 1 on the first diagram that does not reach `ok: true` with every artifact
check passed at showcase quality. Diagnostics are printed as archify emits them
(machine-readable subject + supportedFixes) so the implementer agent can act on
them without reading renderer source.

Usage:
    python3 tools/check_diagrams.py --archify /opt/archify/archify/bin/archify.mjs docs/diagrams/archify
    python3 tools/check_diagrams.py --archify ... --deliver --out rendered docs/diagrams/archify
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

TYPES = {"architecture", "workflow", "sequence", "dataflow", "lifecycle"}


def _run(args: list[str]) -> dict[str, Any]:
    # The argv is built from this script's own flags and the file list it
    # globbed; nothing here comes from a PR body or a network response.
    proc = subprocess.run(args, capture_output=True, text=True, check=False)  # noqa: S603
    text = proc.stdout.strip() or proc.stderr.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {"ok": False, "diagnostics": [{"severity": "error", "code": "cli/unparseable", "message": text[:2000]}]}


def _type_of(path: Path) -> str:
    parts = path.name.split(".")
    if len(parts) < 3 or parts[-2] not in TYPES:
        raise SystemExit(f"error: {path} must be named <name>.<type>.json with type in {sorted(TYPES)}")
    declared = json.loads(path.read_text(encoding="utf-8")).get("diagram_type")
    if declared != parts[-2]:
        raise SystemExit(f"error: {path} is named {parts[-2]} but declares diagram_type={declared!r}")
    return parts[-2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archify", required=True, help="path to bin/archify.mjs")
    ap.add_argument("--deliver", action="store_true", help="also deliver HTML and run visual-check")
    ap.add_argument("--out", default="rendered")
    ap.add_argument("directory")
    ns = ap.parse_args()

    files = sorted(Path(ns.directory).glob("*.json"))
    if not files:
        print(f"error: no diagrams under {ns.directory}", file=sys.stderr)
        return 1

    failed = 0
    for f in files:
        kind = _type_of(f)
        res = _run(["node", ns.archify, "validate", kind, str(f), "--quality", "showcase", "--json"])
        checks = res.get("checks") or []
        passed = sum(1 for c in checks if c.get("ok"))
        comp = (res.get("composition") or {}).get("summary") or {}
        ok = (
            bool(res.get("ok"))
            and checks
            and passed == len(checks)
            and comp.get("errors", 1) == 0
            and comp.get("warnings", 1) == 0
        )
        print(
            f"{'ok  ' if ok else 'fail'} validate {f} ({passed}/{len(checks)} checks, "
            f"composition errors={comp.get('errors')} warnings={comp.get('warnings')})"
        )
        for d in res.get("diagnostics", []):
            print(f"      {d.get('severity')} {d.get('code')}: {d.get('message')}")
            if d.get("supportedFixes"):
                print(f"        fixes: {d['supportedFixes']}")
        if not ok:
            failed += 1
            continue

        if ns.deliver:
            out = Path(ns.out) / (f.name.removesuffix(".json") + ".html")
            out.parent.mkdir(parents=True, exist_ok=True)
            res = _run(["node", ns.archify, "deliver", kind, str(f), str(out), "--quality", "showcase", "--json"])
            print(f"{'ok  ' if res.get('ok') else 'fail'} deliver  {out}")
            if not res.get("ok"):
                failed += 1
                continue
            if os.environ.get("ARCHIFY_CHROME"):
                res = _run(["node", ns.archify, "visual-check", str(out), "--json"])
                codes = sorted({d.get("code") for d in res.get("diagnostics", [])})
                # viewport-overflow is informational for pages that scroll to
                # their cards. visual-check-runtime and chrome-unavailable mean
                # the browser could not run at all - an environment problem,
                # not a diagram defect - so they are reported loudly but do not
                # fail a diagram that deliver already proved deterministically
                # (archify's delivery contract keeps those two claims apart).
                soft = {"viewer/viewport-overflow", "viewer/visual-check-runtime", "viewer/chrome-unavailable"}
                hard = [c for c in codes if c not in soft]
                env_broken = [c for c in codes if c in {"viewer/visual-check-runtime", "viewer/chrome-unavailable"}]
                status = "fail" if hard else ("warn" if env_broken else "ok  ")
                print(f"{status} browser  {out} {codes}")
                if env_broken:
                    print(
                        "      browser evidence not collected - fix ARCHIFY_CHROME; deterministic checks still passed"
                    )
                if hard:
                    failed += 1

    print(f"{len(files) - failed}/{len(files)} diagrams passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
