#!/usr/bin/env bash
# T11: the Python baseline and the Rust proxy must return semantically equal
# `result` objects against the example fake upstream. Byte differences are
# reported in $OUT/parity-bytes.md and never fail the script.
#
# Needs: cargo build --locked --examples (and the bin), uv sync --locked.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

BIN=${BIN:-target/debug/mctl-runtime}
DRIVER=${DRIVER:-target/debug/examples/bench_driver}
FAKE=${FAKE:-target/debug/examples/fake_upstream}
OUT=${OUT:-$(mktemp -d)}
ALLOW=bench/allowlist.toml
mkdir -p "$OUT"

trap stop_fake EXIT
start_fake

rust_cmd=("$BIN" proxy --upstream "$URL" --allowlist "$ALLOW")
py_cmd=("$VENV/bin/python" -I baseline/thin_adapter.py --upstream "$URL" --allowlist "$ALLOW")

"$DRIVER" parity --upstream "$URL" -- "${rust_cmd[@]}" >"$OUT/parity-rust.jsonl"
"$DRIVER" parity --upstream "$URL" -- "${py_cmd[@]}" >"$OUT/parity-python.jsonl"
"$DRIVER" parity --raw --upstream "$URL" -- "${rust_cmd[@]}" >"$OUT/parity-rust.raw"
"$DRIVER" parity --raw --upstream "$URL" -- "${py_cmd[@]}" >"$OUT/parity-python.raw"

status=0
"$VENV/bin/python" -I - "$OUT/parity-rust.jsonl" "$OUT/parity-python.jsonl" <<'PY' || status=$?
import copy
import json
import sys

EXPECTED_TOOLS = {"mctl_whoami", "deny_tool", "approval_tool", "slow_tool"}


def load(path):
    with open(path, encoding="utf-8") as fh:
        return {json.loads(l)["request"]: json.loads(l) for l in fh if l.strip()}


def differences(a, b):
    bad = []
    if set(a) != set(b):
        bad.append(f"request sets differ: {sorted(set(a) ^ set(b))}")
    for key in sorted(a):
        if key == "initialize":
            continue  # serverInfo and capabilities differ by design
        if a[key] != b.get(key):
            bad.append(f"{key}: {json.dumps(a[key], sort_keys=True)} != {json.dumps(b.get(key), sort_keys=True)}")
    return bad


def sanity(side, data):
    names = {t["name"] for t in data["tools/list"]["result"]["tools"]}
    if names != EXPECTED_TOOLS:
        return [f"{side}: tools/list names {sorted(names)} != {sorted(EXPECTED_TOOLS)}"]
    out = []
    for tool in ("secret_tool", "no_such_tool"):
        res = data[f"tools/call {tool}"]["result"]
        if res.get("isError") is not True or res["structuredContent"]["reason_code"] != "not-eligible":
            out.append(f"{side}: {tool} was not refused as not-eligible")
    return out


rust, python = load(sys.argv[1]), load(sys.argv[2])
problems = sanity("rust", rust) + sanity("python", python) + differences(rust, python)
if problems:
    print("T11 FAILED:")
    for p in problems:
        print("  " + p)
    sys.exit(1)

# Self-check: the comparison must notice a secret_tool entry in the baseline.
tampered = copy.deepcopy(python)
tampered["tools/list"]["result"]["tools"].append({"name": "secret_tool", "inputSchema": {"type": "object"}})
if not differences(rust, tampered):
    print("T11 self-check FAILED: a secret_tool entry in the baseline was not detected")
    sys.exit(3)
print(f"T11 ok: {len(rust) - 1} results semantically equal (initialize excluded), self-check passed")
PY

{
  echo "# Parity byte differences (informational)"
  echo
  echo "Raw compact output of each subject for the T11 sequence. Semantic equality is"
  echo "what T11 enforces; this file records how the bytes differ."
  echo
  if diff -u "$OUT/parity-rust.raw" "$OUT/parity-python.raw" >"$BENCH_TMP/bytes.diff"; then
    echo "No byte differences."
  else
    echo '```diff'
    cat "$BENCH_TMP/bytes.diff"
    echo '```'
  fi
} >"$OUT/parity-bytes.md"
cat "$OUT/parity-bytes.md"
echo "parity output in $OUT"
exit "$status"
