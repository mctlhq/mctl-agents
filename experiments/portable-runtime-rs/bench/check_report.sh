#!/usr/bin/env bash
# T12: report shape. The `## Decision` section must contain exactly one of the
# four decision tokens, the file has exactly one `Recommendation: ` line, and every GO-checklist line must cite a `measurements/`
# path or a test id (T<n>). The script also proves that it fails on a copy with
# two tokens and on a copy with an uncited GO line.
#
# usage: check_report.sh [REPORT]   (default docs/spikes/746-portable-runtime.md)
set -euo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)
REPORT=${1:-$ROOT/docs/spikes/746-portable-runtime.md}
TOKENS='NO_RUNTIME|THIN_ADAPTER|PORTABLE_RUNTIME_RUST|PORTABLE_RUNTIME_OTHER'

# section FILE HEADING-REGEX : the lines under a `## ` heading, up to the next one.
section() {
  awk -v re="$2" '
    /^## / { on = ($0 ~ re); next }
    on { print }
  ' "$1"
}

check() {
  local file=$1 decision go count line bad=0
  decision=$(section "$file" '^## Decision')
  count=$(printf '%s\n' "$decision" | { grep -oE "$TOKENS" || true; } | wc -l | tr -d ' ')
  if [ "$count" != 1 ]; then
    echo "Decision section must contain exactly one token, found $count" >&2
    bad=1
  fi
  count=$(grep -cE '^Recommendation: ' "$file" || true)
  if [ "$count" != 1 ]; then
    echo "report must contain exactly one 'Recommendation: ' line, found $count" >&2
    bad=1
  fi
  go=$(section "$file" '^## GO checklist' | grep -E '^- \*\*(G[1-7]|Rust)' || true)
  if [ "$(printf '%s\n' "$go" | grep -c .)" -lt 8 ]; then
    echo "GO checklist must have lines for G1 to G7 and the Rust clause" >&2
    bad=1
  fi
  while IFS= read -r line; do
    if [ -z "$line" ]; then continue; fi
    if ! grep -qE 'measurements/|T[0-9]+' <<<"$line"; then
      echo "GO line without a measurements/ path or test id: ${line:0:80}" >&2
      bad=1
    fi
  done <<<"$go"
  return "$bad"
}

if [ ! -f "$REPORT" ]; then
  echo "report not found: $REPORT" >&2
  exit 1
fi
check "$REPORT"

tmp=$(mktemp -d)
trap 'rm -rf "$tmp"' EXIT
# Self-check 1: a second token in the Decision section must fail.
awk '{ print } /^## Decision/ { print "THIN_ADAPTER NO_RUNTIME" }' "$REPORT" >"$tmp/two-tokens.md"
if check "$tmp/two-tokens.md" 2>/dev/null; then
  echo "self-check failed: a two-token Decision passed" >&2
  exit 1
fi
# Self-check 1b: a second Recommendation line must fail.
{ cat "$REPORT"; echo 'Recommendation: `NO_RUNTIME`'; } >"$tmp/two-recs.md"
if check "$tmp/two-recs.md" 2>/dev/null; then
  echo "self-check failed: two Recommendation lines passed" >&2
  exit 1
fi
# Self-check 2: an uncited GO line must fail.
sed 's/^- \*\*G2.*/- **G2** met./' "$REPORT" >"$tmp/uncited.md"
if check "$tmp/uncited.md" 2>/dev/null; then
  echo "self-check failed: an uncited GO line passed" >&2
  exit 1
fi
echo "T12 ok: $REPORT"
