# Measurements: ubuntu-latest

Run id: `40028550067`, head: `83f6527a522fc9175645e688c5d1293781e021e0`, runner OS: `ubuntu-latest`.

## Environment

```text
os: Linux 6.17.0-1022-azure (x86_64)
cpu: AMD EPYC 9V45 96-Core Processor
memory_kb: 16373452
cores: 4
rustc: rustc 1.85.0 (4d91de4e4 2025-02-17)
python: Python 3.12.3
uv: uv 0.11.11 (x86_64-unknown-linux-gnu)
run_id: 40028550067
head_sha: 83f6527a522fc9175645e688c5d1293781e021e0
```

## Results

| Subject | Variant | Metric | Value | Unit | Status | Reason | Note |
|---|---|---|---|---|---|---|---|
| rust-proxy | default | build_time | 1.202 | s | measured |  | http-only, TLS not included |
| python-baseline | default | build_time | 0.565 | s | measured |  | uv sync --locked of the full project environment |
| rust-proxy | default | binary_size | 840736 | bytes | measured |  | http-only, TLS not included |
| rust-proxy | default | dependency_lines | 4 | lines | measured | ELF 64-bit LSB pie executable, x86-64, version 1 (SYSV), dynamically linked, interpreter /lib64/ld-linux-x86-64.so.2, BuildID[sha1]=e3f9b89c927c84bbd2302e23234594b8acbe22ef, for GNU/Linux 3.2.0, not s | http-only, TLS not included |
| python-baseline | default | script_size | 7023 | bytes | measured |  |  |
| python-baseline | default | packages_size | 12944 | kB | measured | mcp and its transitive dependencies |  |
| python-baseline | default | dependency_count | 26 | distributions | measured | closure of mcp's requirements |  |
| python-baseline | default | interpreter_size | 40427530 | kB | measured | du of sys.base_prefix (interpreter and stdlib) |  |
| rust-proxy | default | ready_median_ms | 0.474 | ms | measured |  | http-only, TLS not included |
| rust-proxy | default | ready_p95_ms | 0.539 | ms | measured |  | http-only, TLS not included |
| python-baseline | default | ready_median_ms | 293.008 | ms | measured |  |  |
| python-baseline | default | ready_p95_ms | 303.191 | ms | measured |  |  |
| rust-proxy | default | first_call_ms | 0.433 | ms | measured |  | http-only, TLS not included |
| rust-proxy | default | steady_median_ms | 0.119 | ms | measured |  | http-only, TLS not included |
| rust-proxy | default | steady_p95_ms | 0.134 | ms | measured |  | http-only, TLS not included |
| direct | default | first_call_ms | 0.411 | ms | measured |  |  |
| direct | default | steady_median_ms | 0.085 | ms | measured |  |  |
| direct | default | steady_p95_ms | 0.096 | ms | measured |  |  |
| rust-proxy | default | overhead_first_ms | 0.021 | ms | measured | subject minus direct | http-only, TLS not included |
| rust-proxy | default | overhead_median_ms | 0.034 | ms | measured | subject minus direct | http-only, TLS not included |
| rust-proxy | default | overhead_p95_ms | 0.039 | ms | measured | subject minus direct | http-only, TLS not included |
| python-baseline | default | first_call_ms | 4.76 | ms | measured |  |  |
| python-baseline | default | steady_median_ms | 1.357 | ms | measured |  |  |
| python-baseline | default | steady_p95_ms | 1.515 | ms | measured |  |  |
| direct | default | first_call_ms | 0.369 | ms | measured |  |  |
| direct | default | steady_median_ms | 0.087 | ms | measured |  |  |
| direct | default | steady_p95_ms | 0.097 | ms | measured |  |  |
| python-baseline | default | overhead_first_ms | 4.391 | ms | measured | subject minus direct |  |
| python-baseline | default | overhead_median_ms | 1.27 | ms | measured | subject minus direct |  |
| python-baseline | default | overhead_p95_ms | 1.417 | ms | measured | subject minus direct |  |
| rust-proxy | default | rss_idle_kb | 2412.0 | kB | measured |  | http-only, TLS not included |
| rust-proxy | default | rss_peak_call_kb | 2416.0 | kB | measured | max of 20 ms samples during slow_tool | http-only, TLS not included |
| rust-proxy | default | rss_hwm_kb | 2416.0 | kB | measured | VmHWM after slow_tool | http-only, TLS not included |
| python-baseline | default | rss_idle_kb | 56820.0 | kB | measured |  |  |
| python-baseline | default | rss_peak_call_kb | 56864.0 | kB | measured | max of 20 ms samples during slow_tool |  |
| python-baseline | default | rss_hwm_kb | 56864.0 | kB | measured | VmHWM after slow_tool |  |
| python-uv-run | default | ready_median_ms | 308.908 | ms | measured |  |  |
| python-uv-run | default | ready_p95_ms | 318.642 | ms | measured |  |  |
| rust-proxy | tls | build_time | 19.948 | s | measured |  |  |
| rust-proxy | tls | binary_size | 2719056 | bytes | measured |  |  |
| rust-proxy | tls | dependency_lines | 4 | lines | measured | ELF 64-bit LSB pie executable, x86-64, version 1 (SYSV), dynamically linked, interpreter /lib64/ld-linux-x86-64.so.2, BuildID[sha1]=330af3e46a5fe0ff2d65e3216598f9c93995038b, for GNU/Linux 3.2.0, not s |  |
| rust-proxy | tls | ready_median_ms | 0.525 | ms | measured |  |  |
| rust-proxy | tls | ready_p95_ms | 0.603 | ms | measured |  |  |
| rust-proxy | tls | first_call_ms | 0.521 | ms | measured |  |  |
| rust-proxy | tls | steady_median_ms | 0.121 | ms | measured |  |  |
| rust-proxy | tls | steady_p95_ms | 0.157 | ms | measured |  |  |
| direct | tls | first_call_ms | 0.291 | ms | measured |  |  |
| direct | tls | steady_median_ms | 0.082 | ms | measured |  |  |
| direct | tls | steady_p95_ms | 0.093 | ms | measured |  |  |
| rust-proxy | tls | overhead_first_ms | 0.23 | ms | measured | subject minus direct |  |
| rust-proxy | tls | overhead_median_ms | 0.039 | ms | measured | subject minus direct |  |
| rust-proxy | tls | overhead_p95_ms | 0.064 | ms | measured | subject minus direct |  |
| rust-proxy | tls | rss_idle_kb | 2816.0 | kB | measured |  |  |
| rust-proxy | tls | rss_peak_call_kb | 2820.0 | kB | measured | max of 20 ms samples during slow_tool |  |
| rust-proxy | tls | rss_hwm_kb | 2820.0 | kB | measured | VmHWM after slow_tool |  |
| both | default | live | unmeasured |  | unmeasured | not run in this slice (MCTL_RUNTIME_LIVE unset; no credentials in CI) |  |

## Rust proxy: default vs tls

| Metric | Unit | default | tls |
|---|---|---|---|
| binary_size | bytes | 840736 | 2719056 |
| build_time | s | 1.202 | 19.948 |
| dependency_lines | lines | 4 | 4 |
| first_call_ms | ms | 0.433 | 0.521 |
| overhead_first_ms | ms | 0.021 | 0.23 |
| overhead_median_ms | ms | 0.034 | 0.039 |
| overhead_p95_ms | ms | 0.039 | 0.064 |
| ready_median_ms | ms | 0.474 | 0.525 |
| ready_p95_ms | ms | 0.539 | 0.603 |
| rss_hwm_kb | kB | 2416.0 | 2820.0 |
| rss_idle_kb | kB | 2412.0 | 2816.0 |
| rss_peak_call_kb | kB | 2416.0 | 2820.0 |
| steady_median_ms | ms | 0.119 | 0.121 |
| steady_p95_ms | ms | 0.134 | 0.157 |
