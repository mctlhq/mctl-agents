# Spike #746 measurements

Status: **code phase.** No bench artifact is committed yet. The shepherd commits
the artifacts of one CI run on the PR's final code head into this directory and
fills in the lines below. Until then every number in
`docs/spikes/746-portable-runtime.md` reads `unmeasured (pending bench run)`.

- Run id: `unmeasured (pending bench run)`
- Head SHA: `unmeasured (pending bench run)`
- Workflow: `.github/workflows/portable-runtime-spike.yml`, jobs `bench` and `cross-build`

## Files the shepherd commits here

| File | Source |
|---|---|
| `ubuntu-latest.json`, `ubuntu-latest.md` | artifact `portable-runtime-measurements-ubuntu-latest` |
| `macos-14.json`, `macos-14.md` | artifact `portable-runtime-measurements-macos-14` |
| `cross-build.json` | the `cross-build` artifacts, merged into one array |
| `parity-bytes.md` | the byte-level parity finding written by `bench/parity.sh` |

The artifact must come from a run on a head whose code matches the final head.
A docs-only commit does not change the measured binaries, so a later docs-only
head is fine.

## Record schema

Each `<os>.json` file is an array of records:

| Field | Meaning |
|---|---|
| `subject` | `rust-proxy`, `python-baseline`, `python-uv-run`, `direct` (plain HTTP, no proxy) or `both` |
| `metric` | for example `build_time`, `binary_size`, `ready_median_ms`, `steady_p95_ms`, `rss_idle_kb` |
| `value` | a number, or `null` when the metric was not measured. A missing metric is never `0`. |
| `unit` | `s`, `ms`, `bytes`, `kB`, `lines`, `distributions` or empty |
| `status` | `measured`, `unmeasured` or `failed` |
| `reason` | why a record is `unmeasured` or `failed`; context for a measured one |
| `note` | `http-only, TLS not included` on every Rust size, build, dependency and startup record |
| `os`, `head_sha`, `run_id` | where and from which commit and run the record came |

The rendered `<os>.md` adds the environment block (CPU, cores, memory, OS,
rustc, Python and uv versions).

## Method

- `ready`: spawn to the `initialize` response, N=20, nearest-rank median and p95.
- `latency`: one handshake, then one first call and N=100 steady-state
  `mctl_whoami` calls. `direct` is the same calls through the crate's own HTTP
  client (`upstream::Upstream::call_tool`); overhead is the subject minus
  `direct`. The Rust proxy opens its upstream session lazily on the first call,
  the Python baseline opens it in the background at start, so the first-call
  figures are not symmetric. The steady-state figures are.
- `rss`: one warm call, a 300 ms pause, then idle RSS; then peak RSS sampled
  every 20 ms during a `slow_tool` call (about 2 s). Linux reads `VmRSS` and
  `VmHWM` from `/proc/<pid>/status`; macOS uses `ps -o rss=`. The Python figure
  is the interpreter process itself.
- The server is spawned with a cleared environment plus `PATH`, `HOME` and
  `MCTL_TOKEN=bench-token-746`.
- Size: `stat` of the release binary; for Python the script, the `mcp`
  requirement closure and `sys.base_prefix`, reported separately.
- Dependencies: line count of `ldd` (`otool -L` on macOS); for Python, the count
  of installed distributions in the `mcp` requirement closure.
- Python is launched as `python -I baseline/thin_adapter.py`; the `uv run`
  cold start is a second number (`python-uv-run`).
- The live run against `api.mctl.ai` is `unmeasured` (not run in this slice).

## TLS limit

The Rust crate is std-only and speaks plain `http://`. Every Rust size, build,
dependency and startup number here is **http-only, TLS not included**. A
TLS-capable binary will be larger, take longer to build and add dependencies.
That variant is unmeasured.
