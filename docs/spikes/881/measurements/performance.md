| op | runc median s | gvisor median s | gvisor vs runc |
|---|---|---|---|
| argo_executor | 45.000 | 55.000 | +22.2% |
| c_compile | 0.039 | 0.104 | +166.7% |
| cancellation | 8.000 | 5.000 | -37.5% |
| dns | 0.015 | 0.029 | +83.9% |
| emptydir_io | 0.479 | 0.532 | +11.0% |
| gh_version | 0.040 | 0.066 | +67.1% |
| git_clone_local | 0.021 | 0.118 | +464.3% |
| go_test | 9.935 | 13.821 | +39.1% |
| go_test_race | 13.085 | 18.348 | +40.2% |
| https_direct | 0.378 | 0.400 | +6.1% |
| https_proxy | 0.377 | 0.402 | +6.8% |
| log_collection | 0.000 | 0.000 | n/a |
| npm_ci | 0.458 | 0.839 | +83.1% |
| uv_pytest_mypy_ruff | 2.287 | 5.502 | +140.6% |

| runtime | startup p50 s | startup p95 s | cpu usage s | memory peak MiB | io bytes MiB |
|---|---|---|---|---|---|
| runc | 1.0 | 2.0 | 61.2 | 616.5 | 450.2 |
| gvisor | 2.0 | 2.0 | n/a | n/a | n/a |
