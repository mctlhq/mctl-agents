# Measurements: ubuntu-latest

Run id: `40002256127`, head: `12f5582b45250b2059324689d244671ada2f31db`, runner OS: `ubuntu-latest`.

## Environment

```text
os: Linux 6.17.0-1022-azure (x86_64)
cpu: AMD EPYC 7763 64-Core Processor
memory_kb: 16373452
cores: 4
rustc: rustc 1.85.0 (4d91de4e4 2025-02-17)
python: Python 3.12.3
uv: uv 0.11.11 (x86_64-unknown-linux-gnu)
run_id: 40002256127
head_sha: 12f5582b45250b2059324689d244671ada2f31db
```

## Results

| Subject | Metric | Value | Unit | Status | Reason | Note |
|---|---|---|---|---|---|---|
| rust-proxy | build_time | 1.964 | s | measured |  | http-only, TLS not included |
| python-baseline | build_time | 0.171 | s | measured |  | uv sync --locked of the full project environment |
| rust-proxy | binary_size | 840824 | bytes | measured |  | http-only, TLS not included |
| rust-proxy | dependency_lines | 4 | lines | measured | ELF 64-bit LSB pie executable, x86-64, version 1 (SYSV), dynamically linked, interpreter /lib64/ld-linux-x86-64.so.2, BuildID[sha1]=531a6052ddc78b8ff068d3517b8ea98599e0678f, for GNU/Linux 3.2.0, not s | http-only, TLS not included |
| python-baseline | script_size | 7023 | bytes | measured |  |  |
| python-baseline | packages_size | 12944 | kB | measured | mcp and its transitive dependencies |  |
| python-baseline | dependency_count | 26 | distributions | measured | closure of mcp's requirements |  |
| python-baseline | interpreter_size | 40427530 | kB | measured | du of sys.base_prefix (interpreter and stdlib) |  |
| rust-proxy | ready_median_ms | 0.639 | ms | measured |  | http-only, TLS not included |
| rust-proxy | ready_p95_ms | 0.71 | ms | measured |  | http-only, TLS not included |
| python-baseline | ready_median_ms | 474.248 | ms | measured |  |  |
| python-baseline | ready_p95_ms | 479.124 | ms | measured |  |  |
| rust-proxy | first_call_ms | 0.611 | ms | measured |  | http-only, TLS not included |
| rust-proxy | steady_median_ms | 0.258 | ms | measured |  | http-only, TLS not included |
| rust-proxy | steady_p95_ms | 0.288 | ms | measured |  | http-only, TLS not included |
| direct | first_call_ms | 0.561 | ms | measured |  |  |
| direct | steady_median_ms | 0.167 | ms | measured |  |  |
| direct | steady_p95_ms | 0.2 | ms | measured |  |  |
| rust-proxy | overhead_first_ms | 0.05 | ms | measured | subject minus direct | http-only, TLS not included |
| rust-proxy | overhead_median_ms | 0.09 | ms | measured | subject minus direct | http-only, TLS not included |
| rust-proxy | overhead_p95_ms | 0.088 | ms | measured | subject minus direct | http-only, TLS not included |
| python-baseline | first_call_ms | 7.961 | ms | measured |  |  |
| python-baseline | steady_median_ms | 2.35 | ms | measured |  |  |
| python-baseline | steady_p95_ms | 2.581 | ms | measured |  |  |
| direct | first_call_ms | 0.577 | ms | measured |  |  |
| direct | steady_median_ms | 0.18 | ms | measured |  |  |
| direct | steady_p95_ms | 0.213 | ms | measured |  |  |
| python-baseline | overhead_first_ms | 7.384 | ms | measured | subject minus direct |  |
| python-baseline | overhead_median_ms | 2.17 | ms | measured | subject minus direct |  |
| python-baseline | overhead_p95_ms | 2.368 | ms | measured | subject minus direct |  |
| rust-proxy | rss_idle_kb | 2348.0 | kB | measured |  | http-only, TLS not included |
| rust-proxy | rss_peak_call_kb | 2352.0 | kB | measured | max of 20 ms samples during slow_tool | http-only, TLS not included |
| rust-proxy | rss_hwm_kb | 2352.0 | kB | measured | VmHWM after slow_tool | http-only, TLS not included |
| python-baseline | rss_idle_kb | 56664.0 | kB | measured |  |  |
| python-baseline | rss_peak_call_kb | 56704.0 | kB | measured | max of 20 ms samples during slow_tool |  |
| python-baseline | rss_hwm_kb | 56704.0 | kB | measured | VmHWM after slow_tool |  |
| python-uv-run | ready_median_ms | 488.423 | ms | measured |  |  |
| python-uv-run | ready_p95_ms | 511.255 | ms | measured |  |  |
| both | live | unmeasured |  | unmeasured | not run in this slice (MCTL_RUNTIME_LIVE unset; no credentials in CI) |  |
