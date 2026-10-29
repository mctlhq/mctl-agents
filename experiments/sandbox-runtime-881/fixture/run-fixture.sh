#!/usr/bin/env bash
# Compatibility fixture for spike #881. Runs inside the agent image as uid 1000
# in an emptyDir workdir. Prints one JSON line per op:
#   {"op": ..., "ok": true|false, "seconds": N, "stderr_tail": "..."}
# Fixtures are vendored under ./fixtures; only dns/https ops use the network.
set -u
HERE="$(cd "$(dirname "$0")" && pwd)"
WORK="${WORKDIR:-$(mktemp -d)}"
mkdir -p "$WORK"
cp -r "$HERE/fixtures" "$WORK/"
cd "$WORK/fixtures" || exit 2

run() { # op cmd...
  local op="$1"; shift
  local err="$WORK/.err.$op"
  local t0 t1 rc
  t0=$(date +%s.%N)
  "$@" >/dev/null 2>"$err"
  rc=$?
  t1=$(date +%s.%N)
  python3 -I - "$op" "$rc" "$t0" "$t1" "$err" <<'PY'
import json, sys
op, rc, t0, t1, errf = sys.argv[1:6]
try:
    tail = open(errf, encoding="utf-8", errors="replace").read()[-400:]
except OSError:
    tail = ""
print(json.dumps({"op": op, "ok": rc == "0", "seconds": round(float(t1) - float(t0), 3), "stderr_tail": tail}))
PY
}

run git_clone_local bash -c 'git init -q src && cd src && git -c user.email=a@b -c user.name=n commit -q --allow-empty -m x && git clone -q . ../clone'
run gh_version gh --version
run uv_pytest_mypy_ruff bash -c 'cd py && uv venv -q .venv && uv pip install -q --python .venv/bin/python --no-index --find-links "${WHEEL_CACHE:-/wheels}" pytest mypy ruff && .venv/bin/pytest -q && .venv/bin/mypy pkg && .venv/bin/ruff check .'
run npm_ci bash -c 'cd js && npm ci --offline --ignore-scripts --cache "${NPM_CACHE:-/npm-cache}"'
run go_test bash -c 'cd go && go test ./...'
run go_test_race bash -c 'cd go && CGO_ENABLED=1 go test -race ./...'
run c_compile bash -c 'cd c && cc -o hello hello.c && ./hello'
run emptydir_io bash -c 'dd if=/dev/zero of=big.bin bs=1M count=256 conv=fsync status=none && cat big.bin >/dev/null'
run dns getent hosts "${DNS_HOST:-github.com}"
run https_direct curl -fsS -o /dev/null --noproxy '*' "${HTTPS_URL:-https://github.com}"
run https_proxy curl -fsS -o /dev/null "${HTTPS_URL:-https://github.com}"
# The cancellation target: the workflow stops the pod while this runs.
run cancel_target sleep "${CANCEL_SLEEP:-1}"
