# Measurements: macos-14

Run id: `40002256127`, head: `12f5582b45250b2059324689d244671ada2f31db`, runner OS: `macos-14`.

## Environment

```text
os: Darwin 23.6.0 (arm64)
cpu: Apple M1 (Virtual)
memory_bytes: 7516192768
cores: 3
rustc: rustc 1.85.0 (4d91de4e4 2025-02-17)
python: Python 3.12.10
uv: uv 0.11.11 (ed7b06001 2026-05-06 aarch64-apple-darwin)
run_id: 40002256127
head_sha: 12f5582b45250b2059324689d244671ada2f31db
```

## Results

| Subject | Metric | Value | Unit | Status | Reason | Note |
|---|---|---|---|---|---|---|
| rust-proxy | build_time | 4.296 | s | measured |  | http-only, TLS not included |
| python-baseline | build_time | 0.211 | s | measured |  | uv sync --locked of the full project environment |
| rust-proxy | binary_size | 762224 | bytes | measured |  | http-only, TLS not included |
| rust-proxy | dependency_lines | 1 | lines | measured | Mach-O 64-bit executable arm64 | http-only, TLS not included |
| python-baseline | script_size | 7023 | bytes | measured |  |  |
| python-baseline | packages_size | 12305 | kB | measured | mcp and its transitive dependencies |  |
| python-baseline | dependency_count | 26 | distributions | measured | closure of mcp's requirements |  |
| python-baseline | interpreter_size | 279558 | kB | measured | du of sys.base_prefix (interpreter and stdlib) |  |
| rust-proxy | ready_median_ms | 1.202 | ms | measured |  | http-only, TLS not included |
| rust-proxy | ready_p95_ms | 2.474 | ms | measured |  | http-only, TLS not included |
| python-baseline | ready_median_ms | 329.988 | ms | measured |  |  |
| python-baseline | ready_p95_ms | 436.299 | ms | measured |  |  |
| rust-proxy | first_call_ms | 0.887 | ms | measured |  | http-only, TLS not included |
| rust-proxy | steady_median_ms | 0.253 | ms | measured |  | http-only, TLS not included |
| rust-proxy | steady_p95_ms | 0.496 | ms | measured |  | http-only, TLS not included |
| direct | first_call_ms | 0.921 | ms | measured |  |  |
| direct | steady_median_ms | 0.162 | ms | measured |  |  |
| direct | steady_p95_ms | 0.284 | ms | measured |  |  |
| rust-proxy | overhead_first_ms | -0.034 | ms | measured | subject minus direct | http-only, TLS not included |
| rust-proxy | overhead_median_ms | 0.092 | ms | measured | subject minus direct | http-only, TLS not included |
| rust-proxy | overhead_p95_ms | 0.213 | ms | measured | subject minus direct | http-only, TLS not included |
| python-baseline | first_call_ms | 9.628 | ms | measured |  |  |
| python-baseline | steady_median_ms | 3.271 | ms | measured |  |  |
| python-baseline | steady_p95_ms | 5.52 | ms | measured |  |  |
| direct | first_call_ms | 0.689 | ms | measured |  |  |
| direct | steady_median_ms | 0.216 | ms | measured |  |  |
| direct | steady_p95_ms | 0.283 | ms | measured |  |  |
| python-baseline | overhead_first_ms | 8.939 | ms | measured | subject minus direct |  |
| python-baseline | overhead_median_ms | 3.056 | ms | measured | subject minus direct |  |
| python-baseline | overhead_p95_ms | 5.237 | ms | measured | subject minus direct |  |
| rust-proxy | rss_idle_kb | 1920.0 | kB | measured |  | http-only, TLS not included |
| rust-proxy | rss_peak_call_kb | 1920.0 | kB | measured | max of 20 ms samples during slow_tool | http-only, TLS not included |
| rust-proxy | rss_hwm_kb | unmeasured | kB | unmeasured | high-water mark not available on this OS | http-only, TLS not included |
| python-baseline | rss_idle_kb | 58960.0 | kB | measured |  |  |
| python-baseline | rss_peak_call_kb | 58976.0 | kB | measured | max of 20 ms samples during slow_tool |  |
| python-baseline | rss_hwm_kb | unmeasured | kB | unmeasured | high-water mark not available on this OS |  |
| python-uv-run | ready_median_ms | 461.077 | ms | measured |  |  |
| python-uv-run | ready_p95_ms | 538.769 | ms | measured |  |  |
| both | live | unmeasured |  | unmeasured | not run in this slice (MCTL_RUNTIME_LIVE unset; no credentials in CI) |  |
