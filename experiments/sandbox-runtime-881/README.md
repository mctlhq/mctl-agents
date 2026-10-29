# Spike #881 harness (Phase A)

Disposable harness comparing hardened runc with gVisor (runsc, systrap) for DevLoop agent pods.
Report: `docs/spikes/881-pod-sandbox-runtime.md`.

- `fixture/run-fixture.sh`: compatibility ops, one JSON line each; fixtures are vendored.
- `probes/negative-probes.sh`: safe read-only probes (no exploit code).
- `k8s/`: gVisor `RuntimeClass`, containerd template patch, fixture `WorkflowTemplate`, runner/executor
  ServiceAccounts and RBAC (`rbac.yaml`), Squid proxy.
- `Dockerfile.fixture`: spike-only layer adding a wheelhouse to the agent image for the offline uv op.
- `dispatch.sh`: submits N alternating runc/gVisor fixture passes plus a cancellation pass each, waits for
  any terminal phase and records Argo executor, log collection, cancellation and startup per pass.
- `collect.py`: JSON lines to `measurements/*.json`, a scorecard that keeps every pass
  (`fail:<k>/<N>` when any pass failed or left no record) and a per-op median/overhead table
  (`python -I collect.py --out DIR --runs N runc=a.log gvisor=b.log`).
- `.github/workflows/spike-881-sandbox.yml`: `workflow_dispatch` only, `contents: read`.

Phase A does NOT dispatch the workflow (it cannot run from a branch where it is not on `main`).
Phase B dispatches it from `main`. Before that run, verify the k3s, gVisor and Argo chart pins and
checksums in the workflow; they were not exercised in Phase A. The runner is not a cx43: kernel and CPU
differ, so report relative deltas only. Nothing here touches MCTL production.
