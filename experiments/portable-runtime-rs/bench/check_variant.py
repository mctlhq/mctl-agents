"""T4: schema check of the bench records (`variant` field and the TLS note).

usage: python -I check_variant.py [--self-check] [FILE.json ...]

Rules, per record:
- `variant` is `default` or `tls`;
- a `tls` record has no `http-only` note;
- a `default` rust-proxy record with status `measured` has the `http-only` note.

`--self-check` first proves the checker fails on bad samples and passes on a
good one, then checks the FILEs. Stdlib only.
"""

import json
import sys

NOTE = "http-only, TLS not included"
VARIANTS = ("default", "tls")


def check_records(records):
    errors = []
    for i, rec in enumerate(records):
        where = f"record {i} ({rec.get('subject')}/{rec.get('metric')})"
        variant = rec.get("variant")
        note = rec.get("note") or ""
        if variant not in VARIANTS:
            errors.append(f"{where}: variant must be one of {VARIANTS}, got {variant!r}")
            continue
        if variant == "tls" and "http-only" in note:
            errors.append(f"{where}: tls record carries the http-only note")
        if (
            variant == "default"
            and rec.get("subject") == "rust-proxy"
            and rec.get("status") == "measured"
            and NOTE not in note
        ):
            errors.append(f"{where}: default rust-proxy record lacks the http-only note")
    return errors


def rec(subject, metric, variant, note=None, status="measured"):
    r = {"subject": subject, "metric": metric, "value": 1, "status": status}
    if variant is not None:
        r["variant"] = variant
    if note is not None:
        r["note"] = note
    return r


def self_check():
    good = [
        rec("rust-proxy", "binary_size", "default", NOTE),
        rec("rust-proxy", "binary_size", "tls"),
        rec("python-baseline", "script_size", "default"),
        rec("rust-proxy", "driver_run", "default", status="failed"),
    ]
    bad = {
        "missing variant": [rec("rust-proxy", "binary_size", None, NOTE)],
        "unknown variant": [rec("rust-proxy", "binary_size", "full", NOTE)],
        "tls with note": [rec("rust-proxy", "binary_size", "tls", NOTE)],
        "default without note": [rec("rust-proxy", "binary_size", "default")],
    }
    if check_records(good):
        print("self-check failed: the good sample was rejected", file=sys.stderr)
        return False
    for name, sample in bad.items():
        if not check_records(sample):
            print(f"self-check failed: {name} passed", file=sys.stderr)
            return False
    return True


def main(argv):
    args = argv[1:]
    do_self = "--self-check" in args
    files = [a for a in args if a != "--self-check"]
    if not do_self and not files:
        print(__doc__, file=sys.stderr)
        return 2
    if do_self and not self_check():
        return 1
    status = 0
    for path in files:
        with open(path, encoding="utf-8") as fh:
            records = json.load(fh)
        errors = check_records(records)
        for e in errors:
            print(f"{path}: {e}", file=sys.stderr)
        if errors:
            status = 1
    if status == 0:
        print("T4 ok" + (f": {', '.join(files)}" if files else " (self-check)"))
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv))
