# Measurements: macos-14

Run id: `40028550067`, head: `83f6527a522fc9175645e688c5d1293781e021e0`, runner OS: `macos-14`.

## Environment

```text
os: Darwin 23.6.0 (arm64)
cpu: Apple M1 (Virtual)
memory_bytes: 7516192768
cores: 3
rustc: rustc 1.85.0 (4d91de4e4 2025-02-17)
python: Python 3.12.10
uv: uv 0.11.11 (ed7b06001 2026-05-06 aarch64-apple-darwin)
run_id: 40028550067
head_sha: 83f6527a522fc9175645e688c5d1293781e021e0
```

## Results

| Subject | Variant | Metric | Value | Unit | Status | Reason | Note |
|---|---|---|---|---|---|---|---|
| rust-proxy | default | build_time | 2.17 | s | measured |  | http-only, TLS not included |
| python-baseline | default | build_time | 0.236 | s | measured |  | uv sync --locked of the full project environment |
| rust-proxy | default | binary_size | 762128 | bytes | measured |  | http-only, TLS not included |
| rust-proxy | default | dependency_lines | 1 | lines | measured | Mach-O 64-bit executable arm64 | http-only, TLS not included |
| python-baseline | default | script_size | 7023 | bytes | measured |  |  |
| python-baseline | default | packages_size | 12305 | kB | measured | mcp and its transitive dependencies |  |
| python-baseline | default | dependency_count | 26 | distributions | measured | closure of mcp's requirements |  |
| python-baseline | default | interpreter_size | 279558 | kB | measured | du of sys.base_prefix (interpreter and stdlib) |  |
| rust-proxy | default | ready_median_ms | 1.735 | ms | measured |  | http-only, TLS not included |
| rust-proxy | default | ready_p95_ms | 3.134 | ms | measured |  | http-only, TLS not included |
| python-baseline | default | ready_median_ms | 381.206 | ms | measured |  |  |
| python-baseline | default | ready_p95_ms | 532.351 | ms | measured |  |  |
| rust-proxy | default | first_call_ms | 1.003 | ms | measured |  | http-only, TLS not included |
| rust-proxy | default | steady_median_ms | 0.248 | ms | measured |  | http-only, TLS not included |
| rust-proxy | default | steady_p95_ms | 0.4 | ms | measured |  | http-only, TLS not included |
| direct | default | first_call_ms | 0.639 | ms | measured |  |  |
| direct | default | steady_median_ms | 0.191 | ms | measured |  |  |
| direct | default | steady_p95_ms | 0.283 | ms | measured |  |  |
| rust-proxy | default | overhead_first_ms | 0.365 | ms | measured | subject minus direct | http-only, TLS not included |
| rust-proxy | default | overhead_median_ms | 0.057 | ms | measured | subject minus direct | http-only, TLS not included |
| rust-proxy | default | overhead_p95_ms | 0.118 | ms | measured | subject minus direct | http-only, TLS not included |
| python-baseline | default | first_call_ms | 10.091 | ms | measured |  |  |
| python-baseline | default | steady_median_ms | 3.155 | ms | measured |  |  |
| python-baseline | default | steady_p95_ms | 5.6 | ms | measured |  |  |
| direct | default | first_call_ms | 0.94 | ms | measured |  |  |
| direct | default | steady_median_ms | 0.185 | ms | measured |  |  |
| direct | default | steady_p95_ms | 0.318 | ms | measured |  |  |
| python-baseline | default | overhead_first_ms | 9.151 | ms | measured | subject minus direct |  |
| python-baseline | default | overhead_median_ms | 2.97 | ms | measured | subject minus direct |  |
| python-baseline | default | overhead_p95_ms | 5.282 | ms | measured | subject minus direct |  |
| rust-proxy | default | rss_idle_kb | 2032.0 | kB | measured |  | http-only, TLS not included |
| rust-proxy | default | rss_peak_call_kb | 2032.0 | kB | measured | max of 20 ms samples during slow_tool | http-only, TLS not included |
| rust-proxy | default | rss_hwm_kb | unmeasured | kB | unmeasured | high-water mark not available on this OS | http-only, TLS not included |
| python-baseline | default | rss_idle_kb | 59136.0 | kB | measured |  |  |
| python-baseline | default | rss_peak_call_kb | 59168.0 | kB | measured | max of 20 ms samples during slow_tool |  |
| python-baseline | default | rss_hwm_kb | unmeasured | kB | unmeasured | high-water mark not available on this OS |  |
| python-uv-run | default | ready_median_ms | 485.779 | ms | measured |  |  |
| python-uv-run | default | ready_p95_ms | 627.983 | ms | measured |  |  |
| rust-proxy | tls | build_time | 21.939 | s | measured |  |  |
| rust-proxy | tls | binary_size | 2289712 | bytes | measured |  |  |
| rust-proxy | tls | dependency_lines | 2 | lines | measured | Mach-O 64-bit executable arm64 |  |
| rust-proxy | tls | ready_median_ms | 1.139 | ms | measured |  |  |
| rust-proxy | tls | ready_p95_ms | 2.295 | ms | measured |  |  |
| rust-proxy | tls | first_call_ms | 7.817 | ms | measured |  |  |
| rust-proxy | tls | steady_median_ms | 0.276 | ms | measured |  |  |
| rust-proxy | tls | steady_p95_ms | 0.409 | ms | measured |  |  |
| direct | tls | first_call_ms | 0.603 | ms | measured |  |  |
| direct | tls | steady_median_ms | 0.21 | ms | measured |  |  |
| direct | tls | steady_p95_ms | 0.288 | ms | measured |  |  |
| rust-proxy | tls | overhead_first_ms | 7.214 | ms | measured | subject minus direct |  |
| rust-proxy | tls | overhead_median_ms | 0.066 | ms | measured | subject minus direct |  |
| rust-proxy | tls | overhead_p95_ms | 0.121 | ms | measured | subject minus direct |  |
| rust-proxy | tls | rss_idle_kb | 2128.0 | kB | measured |  |  |
| rust-proxy | tls | rss_peak_call_kb | 2128.0 | kB | measured | max of 20 ms samples during slow_tool |  |
| rust-proxy | tls | rss_hwm_kb | unmeasured | kB | unmeasured | high-water mark not available on this OS |  |
| both | default | live | unmeasured |  | unmeasured | not run in this slice (MCTL_RUNTIME_LIVE unset; no credentials in CI) |  |

## Rust proxy: default vs tls

| Metric | Unit | default | tls |
|---|---|---|---|
| binary_size | bytes | 762128 | 2289712 |
| build_time | s | 2.17 | 21.939 |
| dependency_lines | lines | 1 | 2 |
| first_call_ms | ms | 1.003 | 7.817 |
| overhead_first_ms | ms | 0.365 | 7.214 |
| overhead_median_ms | ms | 0.057 | 0.066 |
| overhead_p95_ms | ms | 0.118 | 0.121 |
| ready_median_ms | ms | 1.735 | 1.139 |
| ready_p95_ms | ms | 3.134 | 2.295 |
| rss_hwm_kb | kB | unmeasured | unmeasured |
| rss_idle_kb | kB | 2032.0 | 2128.0 |
| rss_peak_call_kb | kB | 2032.0 | 2128.0 |
| steady_median_ms | ms | 0.248 | 0.276 |
| steady_p95_ms | ms | 0.4 | 0.409 |
