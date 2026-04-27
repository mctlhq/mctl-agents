#!/bin/sh
# entrypoint.sh — link runtime state from STATE_DIR into baked-in agents/
#
# Usage in Argo workflow:
#   - Mount mctl-gitops clone at /workdir
#   - Set STATE_DIR=/workdir/agents-state
#   - Run image with default CMD (python -m orchestrator.run_all)
#
# After the run, the workflow commits /workdir/agents-state and pushes back.
#
# When STATE_DIR is unset (local dev / CI / docker run for testing), the
# baked-in agents/<svc>/inbox|proposals/ + agents/_mentor/digest/ are used as-is
# and any output stays inside the ephemeral container.

set -e

if [ -n "${STATE_DIR}" ]; then
  echo "→ Linking runtime state from ${STATE_DIR}"
  mkdir -p "${STATE_DIR}/_mentor/digest"
  for svc_dir in /app/agents/*/; do
    name=$(basename "$svc_dir")
    [ "$name" = "_mentor" ] && continue
    mkdir -p "${STATE_DIR}/${name}/inbox" "${STATE_DIR}/${name}/proposals"
    rm -rf "${svc_dir}inbox" "${svc_dir}proposals"
    ln -s "${STATE_DIR}/${name}/inbox" "${svc_dir}inbox"
    ln -s "${STATE_DIR}/${name}/proposals" "${svc_dir}proposals"
  done
  rm -rf /app/agents/_mentor/digest
  ln -s "${STATE_DIR}/_mentor/digest" /app/agents/_mentor/digest
  echo "✓ State linked"
fi

exec "$@"
