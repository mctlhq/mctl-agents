#!/usr/bin/env bash
# Shared helpers for parity.sh and measure.sh (#746 slice 3). Source it.
# Sets the paths and starts the example fake upstream.

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)   # experiments/portable-runtime-rs
ROOT=$(cd "$HERE/../.." && pwd)                          # repository root
VENV=${VENV:-$ROOT/.venv}
cd "$HERE"

BENCH_TMP=$(mktemp -d)
FAKE_PID=""
URL=""

# Starts the fake upstream from $FAKE and sets $URL. The fake exits when its
# stdin (a fifo held open on fd 9) closes.
start_fake() {
  mkfifo "$BENCH_TMP/hold"
  "$FAKE" <"$BENCH_TMP/hold" >"$BENCH_TMP/url" &
  FAKE_PID=$!
  exec 9>"$BENCH_TMP/hold"
  local i
  for i in $(seq 1 100); do
    if [ -s "$BENCH_TMP/url" ]; then break; fi
    sleep 0.1
  done
  URL=$(head -n 1 "$BENCH_TMP/url")
  if [ -z "$URL" ]; then
    echo "fake upstream did not print a URL" >&2
    return 1
  fi
}

stop_fake() {
  exec 9>&- || true
  if [ -n "$FAKE_PID" ]; then
    wait "$FAKE_PID" 2>/dev/null || true
  fi
  rm -rf "$BENCH_TMP"
}
