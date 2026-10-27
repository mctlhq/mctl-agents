#!/usr/bin/env bash
# Operator script for spike #670. NOT run in CI and nothing imports it.
#
# Usage: run_explainer.sh <proposal_dir> <target_repo_clone> <sha> <out_dir> [focus]
#   proposal_dir        dir with requirements.md, design.md, tasks.md
#   target_repo_clone   clone of the target repo
#   sha                 40-hex commit to read (the artifact binds to it)
#   out_dir             receives decision-context.md and measurements.json
#   focus               optional: authz|persistence|retry|migration|execution-path
#
# The Claude Agent SDK session must be limited to read-only tools: Read, Glob,
# Grep. Record latency, input/output tokens, cost and files read into
# measurements.json; write `unobserved` for anything not measured.
set -euo pipefail
proposal_dir=${1:?proposal_dir}
clone=${2:?target_repo_clone}
sha=${3:?sha}
out=${4:?out_dir}
focus=${5:-}
here=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$out"
git -C "$clone" checkout --detach "$sha"
# Replace with the operator's Agent SDK invocation; prompt is "$here/explain.md".
# Until it is replaced nothing is generated, so nothing is measured: every
# value below stays `unobserved` rather than timing this placeholder.
echo "invoke the explainer with prompt $here/explain.md, focus='${focus}', cwd=$clone, proposal=$proposal_dir" >&2
cat > "$out/measurements.json" <<JSON
{"latency_s": "unobserved", "input_tokens": "unobserved", "output_tokens": "unobserved", "cost_usd": "unobserved", "files_read": "unobserved"}
JSON
