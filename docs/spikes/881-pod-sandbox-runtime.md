# Spike #881: does DevLoop need a stronger pod sandbox than hardened runc?

STATUS: DRAFT

## 1. Source and limits

- Phase A of an owner-split spike: analysis, harness and draft report. Phase B (measurement) starts only
  after this PR is merged to `main`, because `.github/workflows/spike-881-sandbox.yml` cannot be
  dispatched from a branch where it is not yet on the default branch. It was NOT dispatched here.
- Evidence: `cwft-mctl-agents-implement.yaml` and `cwft-mctl-agents-investigate.yaml` in mctl-gitops
  (read from the gitops checkout): non-root uid 1000, `seccompProfile: RuntimeDefault`,
  `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, no SA token, per-pod `emptyDir`,
  `activeDeadlineSeconds: 7200`. No `RuntimeClass` or `runtimeClassName` exists anywhere, so agent pods
  use the k3s-embedded containerd default runc handler.
- Workers: `mctl-preprod-worker-cx43-fsn1-*` (local-path-provisioner manifest), k3s 1.34, Argo chart 0.47.5.
- Not observed: k3s patch version, containerd version, node kernel, `/dev/kvm`, vmx/svm flag. No
  node access was available to this run.
- Owner decision 2026-10-29: VM runtimes (Kata, Firecracker) are out of scope; no KVM host exists.
  Outcomes are `KEEP_HARDENED_RUNC` or `ADOPT_GVISOR` only.

## 2. Feasibility matrix

| id | runtime | vmm | needs_kvm | kvm_observed | k3s_integration | status | evidence |
|---|---|---|---|---|---|---|---|
| R0 | runc | none | no | no | default handler, no RuntimeClass | not-exercised | production CWFTs have no runtimeClassName; baseline measured in Phase B |
| G1 | gVisor runsc systrap | none | no | no | config.toml.tmpl runsc handler plus RuntimeClass gvisor (handler runsc), optional nodeSelector | not-exercised | harness in experiments/sandbox-runtime-881; runs in Phase B |
| G2 | gVisor runsc KVM platform | kvm | yes | not observed | config.toml.tmpl runsc handler with platform=kvm plus RuntimeClass | infeasible-no-kvm | /dev/kvm not observed on cx43 workers; out of scope |
| K1 | Kata | qemu or cloud-hypervisor | yes | not observed | config.toml.tmpl kata handler plus RuntimeClass | infeasible-no-kvm | no KVM host; VM runtimes out of scope (owner decision) |
| K2 | Kata | firecracker | yes | not observed | config.toml.tmpl kata-fc handler, devmapper snapshotter, RuntimeClass | infeasible-no-kvm | no KVM host; VM runtimes out of scope (owner decision) |
| F1 | firecracker-containerd direct | firecracker | yes | not observed | separate containerd runtime plus RuntimeClass | infeasible-no-kvm | no KVM host; VM runtimes out of scope (owner decision) |

## 3. Threat-to-control map

| id | threat | existing_controls | sandbox_reduces | residual |
|---|---|---|---|---|
| T1 | kernel or runtime escape to the node and co-located tenant pods, Vault, kubelet credentials | seccomp RuntimeDefault, drop ALL, non-root, no privilege escalation (#1717) | yes: gVisor interposes a user-space kernel | shared single cluster; co-location of model-driven untrusted code with other workloads not ruled out |
| T2 | credential exfiltration over egress | #807 G1/G2, #830 detection, Squid proxy | no | unchanged |
| T3 | repository-supplied hooks and settings | #868 | no | in-pod, unchanged |
| T4 | Kubernetes API lateral movement | automountServiceAccountToken false, #807 G3 RBAC | no | unchanged |
| T5 | shell misuse inside the pod | #869 | no | in-pod, unchanged |
| T6 | node resource exhaustion, noisy neighbour | #807 G6 quotas, pod limits | partial: sandbox adds overhead accounting only | cgroup limits remain the control |
| T7 | provider-side WebSearch exfiltration | #870 | no | outside the pod |
| T8 | host information disclosure via /proc, /sys, kernel version | RuntimeDefault seccomp, masked paths | yes: sandbox presents its own kernel view | low severity alone; does not force adoption |

Threat gate: PASSED. T1 is material because model-driven untrusted code runs on workers that are also
shared with other platform and tenant workloads, and no scheduling evidence proves otherwise. Measurement
is therefore required before any adoption decision. A sandbox is defence in depth and does not replace
#868/#869/#870.

## 4. Compatibility scorecard

DRAFT: not measured in Phase A. Cells are filled from the Phase B run. VM runtimes are out of scope
(owner amendment), so only runc and gVisor are scored. Argo executor, artifact/log collection and
cancellation are recorded by `experiments/sandbox-runtime-881/dispatch.sh`, the other ops by
`fixture/run-fixture.sh`. A cell is `pass` only when all N passes succeeded; a pass that failed or
produced no record makes it `fail:<k>/<N> ...`.

| op | runc | gvisor | notes |
|---|---|---|---|
| git/gh | not-exercised | not-exercised | |
| uv/pytest/mypy/ruff | not-exercised | not-exercised | |
| npm | not-exercised | not-exercised | |
| go test | not-exercised | not-exercised | |
| go test -race | not-exercised | not-exercised | needs cgo and ptrace-free shadow memory |
| C compile | not-exercised | not-exercised | |
| DNS/HTTPS/proxy | not-exercised | not-exercised | |
| Argo executor | not-exercised | not-exercised | init/wait/emissary |
| artifact/log collection | not-exercised | not-exercised | |
| cancellation | not-exercised | not-exercised | |

## 5. Performance

Not measured in Phase A. Phase B reports startup p50/p95 (Workflow creation to main container start),
per-op median wall-clock with overhead versus runc (`performance.md` from `collect.py`), and CPU, peak
memory and I/O from the pod's cgroup v2 files (`unavailable` where the runtime does not expose them),
for runc versus gVisor with N=10 alternating passes, run id and head SHA. Adoption gates (all required):

- every critical operation in section 4 passes under gVisor;
- zero runtime-caused failures across N=10;
- median representative fixture wall-clock overhead versus runc is at most 25%.

Startup, CPU, RSS and I/O deltas are reported but do not substitute for the wall-clock gate.
`activeDeadlineSeconds: 7200` is a safety ceiling, not an adoption criterion. The GitHub runner differs
from a cx43 in CPU and kernel, so figures are relative deltas, not production timings.

## 6. Negative probes

Probe script: `experiments/sandbox-runtime-881/probes/negative-probes.sh` (kernel version, /dev, /proc and
/sys visibility, mounts, cgroup, /dev/kvm). It runs inside every fixture pod after the ops. Read-only,
no exploit code. Expected: gVisor reports its own
kernel release and a reduced /dev, /proc and /sys view. Observed: not-exercised (Phase B).

## 7. Cost and operations delta

- Node mutation: the gVisor handler needs `runsc` and `containerd-shim-runsc-v1` installed on each worker
  and a stanza in `config.toml.tmpl`. Cost of the install automation: unknown.
- Upgrade and rollback: k3s regenerates containerd config, so the template must survive k3s upgrades; a
  template mismatch after an upgrade can break the handler. Rollback is removing `runtimeClassName`; runc
  stays the default.
- Observability: gVisor hides host-level process and syscall views; existing node-level detection (#830)
  needs re-checking under gVisor. Effort: unknown.
- Extra node pool or purchase cost: unknown (none is proposed; no KVM pool is analysed).

## 8. Decision

STATUS stays DRAFT. The threat gate passed, so Phase B measurement is required before a final outcome of
`KEEP_HARDENED_RUNC` or `ADOPT_GVISOR`. runc remains the default and rollback path; any rollout would be
an opt-in `runtimeClassName` on one CWFT template in a separate change.

Phase B follow-up (to be filed as exactly one issue by a human; this run cannot create issues): after this
PR is merged to `main`, dispatch `.github/workflows/spike-881-sandbox.yml`, run N=10 alternating
runc/gVisor passes, commit `docs/spikes/881/measurements/` with run id and head SHA, fill sections 4-6,
and finalize the outcome in a separate PR. It inherits every gate above and authorizes no paid or KVM host
and no production mutation.
