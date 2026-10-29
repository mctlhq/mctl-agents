# Spike #881: does DevLoop need a stronger pod sandbox than hardened runc?

STATUS: ADOPT_GVISOR

Conditional: the outcome is a go for a follow-up, not a rollout. See section 8 for the conditions.

## 1. Source and limits

- Phase A (#882) delivered the analysis, harness and draft report. Phase B (#883) is the measurement:
  `.github/workflows/spike-881-sandbox.yml` dispatched from `main`.
- Evidence: `cwft-mctl-agents-implement.yaml` and `cwft-mctl-agents-investigate.yaml` in mctl-gitops
  (read from the gitops checkout): non-root uid 1000, `seccompProfile: RuntimeDefault`,
  `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, no SA token, per-pod `emptyDir`,
  `activeDeadlineSeconds: 7200`. No `RuntimeClass` or `runtimeClassName` exists anywhere, so agent pods
  use the k3s-embedded containerd default runc handler.
- Workers: `mctl-preprod-worker-cx43-fsn1-*` (local-path-provisioner manifest), k3s 1.34, Argo chart 0.47.5.
- Measurement host: a GitHub-hosted `ubuntu-latest` runner with a single-node k3s `v1.34.1+k3s1`, kernel
  `7.0.0-1017-azure`, gVisor release `20251020.0` (systrap). This is not a cx43 worker: figures are
  relative deltas, not production timings.
- Not observed on our workers: k3s patch version, containerd version, node kernel, `/dev/kvm`, vmx/svm
  flag. No node access was available. The runner itself exposes `/dev/kvm` (`crw-rw----`); that is an
  observation about the runner only and says nothing about cx43 workers.
- Owner decision 2026-10-29: VM runtimes (Kata, Firecracker) are out of scope; no KVM host exists.
  Outcomes are `KEEP_HARDENED_RUNC` or `ADOPT_GVISOR` only.

### Phase B runs

| run id | head | runs | result |
|---|---|---|---|
| 40890992826 | ef030af | 10 | INVALID: harness defect. Argo mounts `<executor SA>.service-account-token` when `automountServiceAccountToken: false`, and the Secret `spike881-executor.service-account-token` did not exist, so no fixture pod ever started on either runtime. Every pass waited out its 1800s timeout and the job hit the 300-minute limit. The run measures nothing about runc or gVisor. Fixed in #886. |
| 40951481981 | a4ffd4a | 10 | VALID: the measurement below. Raw files in `docs/spikes/881/measurements/`. |

## 2. Feasibility matrix

| id | runtime | vmm | needs_kvm | kvm_observed | k3s_integration | status | evidence |
|---|---|---|---|---|---|---|---|
| R0 | runc | none | no | no | default handler, no RuntimeClass | measured | baseline, run 40951481981, N=10 |
| G1 | gVisor runsc systrap | none | no | no | config.toml.tmpl runsc handler plus RuntimeClass gvisor (handler runsc), optional nodeSelector | measured | run 40951481981, N=10; sections 4-6 |
| G2 | gVisor runsc KVM platform | kvm | yes | not observed | config.toml.tmpl runsc handler with platform=kvm plus RuntimeClass | infeasible-no-kvm | /dev/kvm not observed on cx43 workers; out of scope |
| K1 | Kata | qemu or cloud-hypervisor | yes | not observed | config.toml.tmpl kata handler plus RuntimeClass | infeasible-no-kvm | no KVM host; VM runtimes out of scope (owner decision) |
| K2 | Kata | firecracker | yes | not observed | config.toml.tmpl kata-fc handler, devmapper snapshotter, RuntimeClass | infeasible-no-kvm | no KVM host; VM runtimes out of scope (owner decision) |
| F1 | firecracker-containerd direct | firecracker | yes | not observed | separate containerd runtime plus RuntimeClass | infeasible-no-kvm | no KVM host; VM runtimes out of scope (owner decision) |
| Q1 | Microsoft Quicksand (QEMU VM per sandbox) | qemu | yes | not observed | none: a Python library that launches QEMU from a process, no RuntimeClass; the executor would become pod -> VM -> agent | infeasible-no-kvm | without KVM it falls back to TCG emulation, documented upstream as 10-50x slower; reviewed 2026-10-29 |

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
| T8 | host information disclosure via /proc, /sys, kernel version | RuntimeDefault seccomp, masked paths | yes: sandbox presents its own kernel view (confirmed in section 6) | low severity alone; does not force adoption |

Threat gate: PASSED. T1 is material because model-driven untrusted code runs on workers that are also
shared with other platform and tenant workloads, and no scheduling evidence proves otherwise. Measurement
is therefore required before any adoption decision. A sandbox is defence in depth and does not replace
#868/#869/#870.

## 4. Compatibility scorecard

Run 40951481981, N=10 alternating passes per runtime (`measurements/scorecard.md`). Argo executor,
artifact/log collection and cancellation are recorded by `experiments/sandbox-runtime-881/dispatch.sh`,
the other ops by `fixture/run-fixture.sh`. A cell is `pass` only when all N passes succeeded; a pass that
failed or produced no record makes it `fail:<k>/<N> ...`.

| op | runc | gvisor | notes |
|---|---|---|---|
| git/gh | pass | pass | `git_clone_local`, `gh_version` |
| uv/pytest/mypy/ruff | pass | pass | |
| npm | pass | pass | `npm ci` from the wheelhouse cache |
| go test | pass | pass | |
| go test -race | pass | pass | needs cgo and ptrace-free shadow memory |
| C compile | pass | pass | |
| DNS/HTTPS/proxy | pass | pass | `dns`, `https_direct`, `https_proxy` (Squid) |
| Argo executor | pass | pass | init/wait/emissary; all 10 fixture Workflows Succeeded on both |
| artifact/log collection | pass | pass | |
| cancellation | pass | pass | the stopped Workflow ends in phase `Failed` by design (`phases.tsv`); the op checks that the started main container was stopped and its pod was gone within the limit |

## 5. Performance

Run 40951481981 (`measurements/performance.md`). Median of N=10 passing records per op.

| op | runc median s | gvisor median s | gvisor vs runc |
|---|---|---|---|
| argo_executor (fixture Workflow wall-clock) | 45.000 | 55.000 | +22.2% |
| go_test_race | 13.085 | 18.348 | +40.2% |
| go_test | 9.935 | 13.821 | +39.1% |
| uv_pytest_mypy_ruff | 2.287 | 5.502 | +140.6% |
| emptydir_io | 0.479 | 0.532 | +11.0% |
| npm_ci | 0.458 | 0.839 | +83.1% |
| https_proxy | 0.377 | 0.402 | +6.8% |
| https_direct | 0.378 | 0.400 | +6.1% |
| git_clone_local | 0.021 | 0.118 | +464.3% |
| c_compile | 0.039 | 0.104 | +166.7% |
| gh_version | 0.040 | 0.066 | +67.1% |
| dns | 0.015 | 0.029 | +83.9% |
| cancellation | 8.000 | 5.000 | -37.5% |

| runtime | startup p50 s | startup p95 s | cpu usage s | memory peak MiB | io bytes MiB |
|---|---|---|---|---|---|
| runc | 1.0 | 2.0 | 61.2 | 616.5 | 450.2 |
| gvisor | 2.0 | 2.0 | n/a | n/a | n/a |

gVisor does not expose the pod cgroup v2 files to the container, so its CPU, peak memory and I/O are
`n/a` (not zero). Per-pass fixture wall-clock was 44-46s on runc and 55-56s on gVisor: no outliers.

Adoption gates (all required, set before measurement):

| gate | result | verdict |
|---|---|---|
| every critical operation in section 4 passes under gVisor | 10/10 rows pass | met |
| zero runtime-caused failures across N=10 | 0/10 runc, 0/10 gVisor | met |
| median representative fixture wall-clock overhead versus runc is at most 25% | +22.2% (45s to 55s) | met, 2.8 points of margin |

Caveat recorded with the gate, not instead of it: the fixture wall-clock includes 15-18s of Argo
and scheduling time outside the measured ops, which dilutes the in-pod overhead. The sum of the in-pod op medians alone is 27.1s on runc
and 40.3s on gVisor, +48.8%. CPU- and filesystem-heavy steps (tests, builds, package installs) carry
most of that; network-bound steps are within +7%. A DevLoop run spends most of its time waiting on the
model over the network, but a run dominated by test suites would see an overhead well above the gate.
Startup, CPU, RSS and I/O deltas are reported but do not substitute for the wall-clock gate.
`activeDeadlineSeconds: 7200` is a safety ceiling, not an adoption criterion.

## 6. Negative probes

Probe script: `experiments/sandbox-runtime-881/probes/negative-probes.sh` (kernel version, /dev, /proc and
/sys visibility, mounts, cgroup, /dev/kvm). It runs inside every fixture pod after the ops. Read-only,
no exploit code. Values were identical across all 10 passes of each runtime.

| probe | runc | gvisor |
|---|---|---|
| kernel_release | `7.0.0-1017-azure` (host kernel) | `4.4.0` (gVisor's own) |
| readable /proc/kallsyms | yes | no |
| readable /proc/modules | yes | no |
| readable /proc/version | yes | yes |
| entries in /sys/kernel | 37 | 0 |
| entries in /sys/firmware | 0 | 0 |
| mountinfo lines | 28 | 21 |
| /dev listing | core fd full mqueue null ptmx pts random shm ... | fd full fuse net null ptmx pts random shm ... |
| /dev/kvm in the pod | absent | absent |
| pid 1 cgroup | `0::/` | gVisor pids controller path |

Expected and observed: gVisor reports its own kernel release and a reduced /proc and /sys view, which is
the T8 reduction in section 3. The probes show the reduced surface only; they do not test an escape.

## 7. Cost and operations delta

- Node mutation: the gVisor handler needs `runsc` and `containerd-shim-runsc-v1` installed on each worker
  and a stanza in `config.toml.tmpl`. Cost of the install automation: unknown.
- Upgrade and rollback: k3s regenerates containerd config, so the template must survive k3s upgrades; a
  template mismatch after an upgrade can break the handler. Rollback is removing `runtimeClassName`; runc
  stays the default.
- Observability: gVisor hides host-level process and syscall views and the pod cgroup files (section 5);
  existing node-level detection (#830) and resource accounting need re-checking under gVisor. Effort:
  unknown.
- Extra node pool or purchase cost: none proposed; no KVM pool is analysed.

## 8. Decision

Outcome: `ADOPT_GVISOR`, conditional. All three adoption gates set in Phase A were met in run
40951481981, and the probes confirm the reduced kernel surface. The gates are applied as written; they
are not re-weighted after seeing the data.

Conditions (all required before any agent pod runs under gVisor):

1. Re-measure on one of our own workers. The runner is not a cx43, and the wall-clock margin is 2.8
   points. Installing `runsc` on a worker is a node mutation and needs its own owner decision and change
   (OpenTofu / CI apply); this spike authorizes none.
2. Start opt-in on one CWFT template, preferring `investigate` over `implement`: implement runs execute
   repository test suites, where the in-pod overhead (+48.8%) applies most.
3. Re-check #830 detection and resource accounting under gVisor before widening.
4. runc remains the default and the rollback path (remove `runtimeClassName`).

Nothing is rolled out by this spike: no node, CWFT or RuntimeClass in the cluster was changed.

Owner decision 2026-10-29: `runsc` will not be installed on our workers. Condition 1 is therefore not
scheduled, no follow-up is opened, and agent pods stay on hardened runc. `ADOPT_GVISOR` records what the
measurement supports; acting on it needs a new owner decision.

Phase B tracking issue: #883. Harness fix that made the measurement possible: #886.
