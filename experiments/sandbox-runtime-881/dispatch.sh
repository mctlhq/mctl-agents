#!/usr/bin/env bash
# Phase B dispatcher for spike #881, run by .github/workflows/spike-881-sandbox.yml
# on a disposable k3s cluster. Usage: dispatch.sh RUNS OUT_DIR IMAGE
#
# For each of RUNS alternating runc/gVisor passes it submits one fixture
# Workflow, waits for ANY terminal phase (a failed pass must not hang until a
# timeout), and appends to OUT_DIR/<runtime>.log:
#   - the pod's fixture and probe JSON lines;
#   - {"op":"argo_executor"}: the Workflow reached Succeeded;
#   - {"op":"log_collection"}: the main container's log was retrieved non-empty;
#   - {"op":"cancellation"}: a mode=cancel Workflow stopped and its pod was gone
#     within CANCEL_LIMIT seconds;
#   - {"metric":"startup"}: seconds from Workflow creation to the main container start.
# Each pass's phase goes to OUT_DIR/phases.tsv, so a failed or timed-out pass
# is visible as such rather than as a short log.
set -euo pipefail
RUNS="$1"; OUT="$2"; IMAGE="$3"
NS=spike881
PASS_LIMIT=${PASS_LIMIT:-1800}
CANCEL_LIMIT=${CANCEL_LIMIT:-300}
mkdir -p "$OUT"
printf 'pass\truntime\tmode\tworkflow\tphase\n' >"$OUT/phases.tsv"

emit() { # runtime json
  printf '%s\n' "$2" >>"$OUT/$1.log"
}

op() { # runtime op ok seconds stderr_tail
  emit "$1" "$(python3 -I -c 'import json, sys; print(json.dumps({"op": sys.argv[1], "ok": sys.argv[2] == "true", "seconds": float(sys.argv[3]), "stderr_tail": sys.argv[4]}))' "$2" "$3" "$4" "$5")"
}

submit() { # runtime mode -> workflow name
  local patch='{}'
  [ "$1" = gvisor ] && patch='{"runtimeClassName":"gvisor"}'
  python3 -I -c '
import json, sys
image, patch, mode = sys.argv[1:4]
print(json.dumps({
    "apiVersion": "argoproj.io/v1alpha1",
    "kind": "Workflow",
    "metadata": {"generateName": "fixture-881-", "namespace": "spike881"},
    "spec": {
        "workflowTemplateRef": {"name": "fixture-881"},
        "arguments": {"parameters": [
            {"name": "agent_image", "value": image},
            {"name": "pod_spec_patch", "value": patch},
            {"name": "mode", "value": mode},
        ]},
    },
}))' "$IMAGE" "$patch" "$2" | kubectl -n "$NS" create -f - -o jsonpath='{.metadata.name}'
}

phase() { kubectl -n "$NS" get workflow "$1" -o jsonpath='{.status.phase}' 2>/dev/null || true; }

wait_terminal() { # workflow limit -> prints the terminal phase, or "timeout"
  local deadline=$((SECONDS + $2)) p
  [ -n "$1" ] || { echo submit-failed; return 0; }
  while [ "$SECONDS" -lt "$deadline" ]; do
    p="$(phase "$1")"
    case "$p" in Succeeded|Failed|Error) echo "$p"; return 0 ;; esac
    sleep 5
  done
  echo timeout
}

pod_of() { kubectl -n "$NS" get pod -l "workflows.argoproj.io/workflow=$1" -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true; }

startup_seconds() { # workflow pod
  python3 -I -c '
import datetime as d, sys
def t(s): return d.datetime.fromisoformat(s.replace("Z", "+00:00"))
try:
    print((t(sys.argv[2]) - t(sys.argv[1])).total_seconds())
except ValueError:
    print("")' \
    "$(kubectl -n "$NS" get workflow "$1" -o jsonpath='{.metadata.creationTimestamp}' 2>/dev/null || true)" \
    "$(kubectl -n "$NS" get pod "$2" -o jsonpath='{.status.containerStatuses[?(@.name=="main")].state.terminated.startedAt}' 2>/dev/null)"
}

fixture_pass() { # i runtime
  local wf p pod t0 logs s
  t0=$SECONDS
  wf="$(submit "$2" fixture)" || wf=""
  p="$(wait_terminal "$wf" "$PASS_LIMIT")"
  printf '%s\t%s\tfixture\t%s\t%s\n' "$1" "$2" "$wf" "$p" >>"$OUT/phases.tsv"
  op "$2" argo_executor "$([ "$p" = Succeeded ] && echo true || echo false)" "$((SECONDS - t0))" "workflow phase: $p"
  pod=""
  [ -n "$wf" ] && pod="$(pod_of "$wf")"
  logs=""
  [ -n "$pod" ] && logs="$(kubectl -n "$NS" logs "$pod" -c main --tail=-1 2>/dev/null || true)"
  op "$2" log_collection "$([ -n "$logs" ] && echo true || echo false)" 0 "$([ -n "$pod" ] && echo "pod $pod" || echo 'no pod')"
  [ -n "$logs" ] && printf '%s\n' "$logs" >>"$OUT/$2.log"
  if [ -n "$pod" ]; then
    s="$(startup_seconds "$wf" "$pod")"
    [ -n "$s" ] && emit "$2" "{\"metric\": \"startup\", \"seconds\": $s}"
  fi
  kubectl -n "$NS" delete workflow "$wf" --wait=true >/dev/null 2>&1 || true
}

cancel_pass() { # i runtime
  local wf deadline pod p t0 ok=false
  wf="$(submit "$2" cancel)" || wf=""
  deadline=$((SECONDS + PASS_LIMIT))
  pod=""
  while [ -n "$wf" ] && [ "$SECONDS" -lt "$deadline" ]; do
    pod="$(pod_of "$wf")"
    if [ -n "$pod" ] && [ "$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.containerStatuses[?(@.name=="main")].state.running.startedAt}' 2>/dev/null)" != "" ]; then
      break
    fi
    case "$(phase "$wf")" in Succeeded|Failed|Error) break ;; esac
    sleep 2
  done
  t0=$SECONDS
  [ -n "$wf" ] && kubectl -n "$NS" patch workflow "$wf" --type merge -p '{"spec":{"shutdown":"Terminate"}}' >/dev/null || true
  p="$(wait_terminal "$wf" "$CANCEL_LIMIT")"
  # Cancelled means the Workflow went terminal AND its pod stopped running
  # (Argo keeps finished pods, so "stopped" is a non-running phase or no pod).
  # A pod that never started measures nothing and counts as a failure.
  if [ -n "$pod" ] && [ "$p" != timeout ]; then
    while [ $((SECONDS - t0)) -lt "$CANCEL_LIMIT" ]; do
      case "$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || echo gone)" in
        Running|Pending) sleep 2 ;;
        *) ok=true; break ;;
      esac
    done
  fi
  printf '%s\t%s\tcancel\t%s\t%s\n' "$1" "$2" "$wf" "$p" >>"$OUT/phases.tsv"
  op "$2" cancellation "$ok" "$((SECONDS - t0))" "phase after terminate: $p"
  kubectl -n "$NS" delete workflow "$wf" --wait=true >/dev/null 2>&1 || true
}

for i in $(seq 1 "$RUNS"); do
  for rt in runc gvisor; do
    fixture_pass "$i" "$rt"
    cancel_pass "$i" "$rt"
  done
done
