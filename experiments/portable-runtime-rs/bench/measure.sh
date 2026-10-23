#!/usr/bin/env bash
# #746 slice 3 measurements: Rust proxy vs Python thin adapter.
#
# Env: OUT (default docs/spikes/746/measurements), RUN_ID, HEAD_SHA,
#      MEASURE_OS (label used for the output file names), VENV,
#      MCTL_RUNTIME_LIVE (only recorded; no live run exists in this slice).
#
# A step that fails writes a `failed` record and the script continues. A
# missing metric is null in the record and is never written as 0. The script
# exits non-zero only if it cannot write its output.
set -euo pipefail
. "$(dirname "${BASH_SOURCE[0]}")/lib.sh"

OUT=${OUT:-$ROOT/docs/spikes/746/measurements}
RUN_ID=${RUN_ID:-local}
HEAD_SHA=${HEAD_SHA:-$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || echo unknown)}
OS_LABEL=${MEASURE_OS:-$(uname -s | tr '[:upper:]' '[:lower:]')-$(uname -m)}
export RUN_ID HEAD_SHA

BIN=target/release/mctl-runtime
DRIVER=target/release/examples/bench_driver
FAKE=target/release/examples/fake_upstream
ALLOW=bench/allowlist.toml
NOTE="http-only, TLS not included"

mkdir -p "$OUT"
REC="$OUT/$OS_LABEL.jsonl"
: >"$REC"
trap stop_fake EXIT

jstr() {
  printf '%s' "$1" | tr '\n\r\t' '   ' | tr -d '\000-\037' | sed 's/\\/\\\\/g; s/"/\\"/g'
}

# rec SUBJECT METRIC VALUE UNIT STATUS REASON [NOTE]; an empty VALUE is null.
rec() {
  local subject=$1 metric=$2 value=$3 unit=$4 status=$5 reason=$6 note=${7:-}
  local note_json=""
  if [ -z "$value" ]; then value=null; fi
  if [ -n "$note" ]; then note_json=",\"note\":\"$(jstr "$note")\""; fi
  printf '{"subject":"%s","metric":"%s","value":%s,"unit":"%s","status":"%s","reason":"%s","os":"%s","head_sha":"%s","run_id":"%s"%s}\n' \
    "$subject" "$metric" "$value" "$unit" "$status" "$(jstr "$reason")" "$OS_LABEL" "$HEAD_SHA" "$RUN_ID" "$note_json" >>"$REC"
}

now_ns() { python3 -c 'import time; print(time.time_ns())'; }

# timed_step SUBJECT METRIC NOTE CMD... : seconds of wall clock, or a failed record.
timed_step() {
  local subject=$1 metric=$2 note=$3
  shift 3
  local start end secs
  start=$(now_ns)
  if "$@" >"$BENCH_TMP/step.log" 2>&1; then
    end=$(now_ns)
    secs=$(awk -v a="$start" -v b="$end" 'BEGIN { printf "%.3f", (b - a) / 1e9 }')
    rec "$subject" "$metric" "$secs" s measured "" "$note"
  else
    rec "$subject" "$metric" "" s failed "command failed: $(tail -n 5 "$BENCH_TMP/step.log")" "$note"
  fi
}

# run_driver SUBJECT ARGS... : appends the driver's JSON lines to the records.
run_driver() {
  local subject=$1
  shift
  if ! "$DRIVER" "$@" >>"$REC" 2>"$BENCH_TMP/driver.err"; then
    rec "$subject" driver_run "" "" failed "driver failed: $(tail -n 3 "$BENCH_TMP/driver.err")"
  fi
}

try() { "$@" 2>/dev/null || echo unknown; }

# ---- environment block -----------------------------------------------------
{
  echo "os: $(try uname -sr) ($(try uname -m))"
  if [ "$(uname -s)" = Darwin ]; then
    echo "cpu: $(try sysctl -n machdep.cpu.brand_string)"
    echo "memory_bytes: $(try sysctl -n hw.memsize)"
  else
    echo "cpu: $(grep -m1 'model name' /proc/cpuinfo 2>/dev/null | cut -d: -f2- | sed 's/^ *//' || echo unknown)"
    echo "memory_kb: $(awk '/MemTotal/ { print $2 }' /proc/meminfo 2>/dev/null || echo unknown)"
  fi
  echo "cores: $(try getconf _NPROCESSORS_ONLN)"
  echo "rustc: $(try rustc --version)"
  echo "python: $(try "$VENV/bin/python" --version)"
  echo "uv: $(try uv --version)"
  echo "run_id: $RUN_ID"
  echo "head_sha: $HEAD_SHA"
} >"$OUT/$OS_LABEL.env"

# ---- build time ------------------------------------------------------------
cargo clean --release >/dev/null 2>&1 || true
timed_step rust-proxy build_time "$NOTE" cargo build --release --locked --bin mctl-runtime
if ! cargo build --release --locked --examples >"$BENCH_TMP/examples.log" 2>&1; then
  rec rust-proxy examples_build "" "" failed "cargo build --examples failed: $(tail -n 5 "$BENCH_TMP/examples.log")"
fi
# A clean environment for the whole project; it carries dependencies beyond the adapter's.
timed_step python-baseline build_time "uv sync --locked of the full project environment" \
  env UV_PROJECT_ENVIRONMENT="$BENCH_TMP/venv" uv sync --locked --project "$ROOT"

# ---- size and dependencies -------------------------------------------------
if [ -f "$BIN" ]; then
  rec rust-proxy binary_size "$(wc -c <"$BIN" | tr -d ' ')" bytes measured "" "$NOTE"
  if [ "$(uname -s)" = Darwin ]; then
    deps=$(otool -L "$BIN" | tail -n +2 | wc -l | tr -d ' ')
  else
    if ldd_out=$(ldd "$BIN" 2>&1); then
      deps=$(wc -l <<<"$ldd_out" | tr -d ' ')
    elif grep -qiE 'not a dynamic executable|statically linked' <<<"$ldd_out"; then
      deps=0   # measured: a static binary has no dynamic dependencies
    else
      deps=""
    fi
  fi
  if [ -n "$deps" ]; then
    rec rust-proxy dependency_lines "$deps" lines measured "$(file -b "$BIN" | cut -c1-200)" "$NOTE"
  else
    rec rust-proxy dependency_lines "" lines failed "ldd failed: $(head -c 200 <<<"$ldd_out")" "$NOTE"
  fi
else
  rec rust-proxy binary_size "" bytes failed "release binary missing" "$NOTE"
  rec rust-proxy dependency_lines "" lines failed "release binary missing" "$NOTE"
fi

rec python-baseline script_size "$(wc -c <baseline/thin_adapter.py | tr -d ' ')" bytes measured ""
if pkg=$("$VENV/bin/python" -I "$HERE/bench/pkg_size.py" 2>"$BENCH_TMP/pkg.err"); then
  read -r dist_count pkg_kb interp_kb <<<"$pkg"
  rec python-baseline packages_size "$pkg_kb" kB measured "mcp and its transitive dependencies"
  rec python-baseline dependency_count "$dist_count" distributions measured "closure of mcp's requirements"
  rec python-baseline interpreter_size "$interp_kb" kB measured "du of sys.base_prefix (interpreter and stdlib)"
else
  rec python-baseline packages_size "" kB failed "metadata walk failed: $(tail -n 3 "$BENCH_TMP/pkg.err")"
  rec python-baseline dependency_count "" distributions failed "metadata walk failed"
  rec python-baseline interpreter_size "" kB failed "metadata walk failed"
fi

# ---- startup, latency, RSS -------------------------------------------------
if [ -x "$FAKE" ] && [ -x "$DRIVER" ] && ! start_fake; then
  for subject in rust-proxy python-baseline python-uv-run; do
    rec "$subject" driver_run "" "" failed "fake upstream did not print a URL within 10 s"
  done
elif [ -x "$FAKE" ] && [ -x "$DRIVER" ]; then
  rust_cmd=("$BIN" proxy --upstream "$URL" --allowlist "$ALLOW")
  py_cmd=("$VENV/bin/python" -I baseline/thin_adapter.py --upstream "$URL" --allowlist "$ALLOW")
  uv_cmd=(uv run --locked --project "$ROOT" python -I "$HERE/baseline/thin_adapter.py" --upstream "$URL" --allowlist "$HERE/$ALLOW")

  for mode in ready latency rss; do
    run_driver rust-proxy "$mode" --subject rust-proxy --note "$NOTE" --upstream "$URL" -- "${rust_cmd[@]}"
    run_driver python-baseline "$mode" --subject python-baseline --upstream "$URL" -- "${py_cmd[@]}"
  done
  # How a user would launch it: through `uv run`.
  run_driver python-uv-run ready --subject python-uv-run --upstream "$URL" -- "${uv_cmd[@]}"
else
  for subject in rust-proxy python-baseline python-uv-run; do
    rec "$subject" driver_run "" "" failed "bench_driver or fake_upstream was not built"
  done
fi

# ---- live run --------------------------------------------------------------
if [ "${MCTL_RUNTIME_LIVE:-}" = 1 ]; then
  rec both live "" "" unmeasured "MCTL_RUNTIME_LIVE=1 set but no live harness exists in this slice"
else
  rec both live "" "" unmeasured "not run in this slice (MCTL_RUNTIME_LIVE unset; no credentials in CI)"
fi

"$(command -v python3)" -I "$HERE/bench/render_table.py" "$REC" "$OUT" "$OS_LABEL" "$OUT/$OS_LABEL.env"
rm -f "$REC"
echo "measurements written to $OUT/$OS_LABEL.{json,md}"
