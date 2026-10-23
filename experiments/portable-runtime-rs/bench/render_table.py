"""Turn a measure.sh JSON-lines file into `<label>.json` and `<label>.md`.

usage: python -I render_table.py RECORDS.jsonl OUT_DIR LABEL [ENV_FILE]

Stdlib only. A record whose status is not `measured` is shown with its status
text, never as a number.
"""

import json
import os
import sys


def cell(rec):
    if rec.get("status") == "measured" and rec.get("value") is not None:
        return str(rec["value"])
    return str(rec.get("status", "unknown"))


def main(argv):
    if len(argv) < 4:
        print(__doc__, file=sys.stderr)
        return 2
    src, out_dir, label = argv[1], argv[2], argv[3]
    env_file = argv[4] if len(argv) > 4 else None
    with open(src, encoding="utf-8") as fh:
        records = [json.loads(line) for line in fh if line.strip()]
    with open(os.path.join(out_dir, f"{label}.json"), "w", encoding="utf-8") as fh:
        json.dump(records, fh, indent=2)
        fh.write("\n")

    lines = [f"# Measurements: {label}", ""]
    if records:
        first = records[0]
        lines.append(f"Run id: `{first.get('run_id')}`, head: `{first.get('head_sha')}`, runner OS: `{first.get('os')}`.")
        lines.append("")
    if env_file and os.path.exists(env_file):
        lines += ["## Environment", "", "```text"]
        with open(env_file, encoding="utf-8") as fh:
            lines += [l.rstrip("\n") for l in fh]
        lines += ["```", ""]
    lines += [
        "## Results",
        "",
        "| Subject | Metric | Value | Unit | Status | Reason | Note |",
        "|---|---|---|---|---|---|---|",
    ]
    for rec in records:
        reason = str(rec.get("reason") or "").replace("|", "/")
        note = str(rec.get("note") or "").replace("|", "/")
        lines.append(
            f"| {rec.get('subject')} | {rec.get('metric')} | {cell(rec)} | {rec.get('unit') or ''} "
            f"| {rec.get('status')} | {reason} | {note} |"
        )
    with open(os.path.join(out_dir, f"{label}.md"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
