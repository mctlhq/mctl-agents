# portable-runtime-rs (EXPERIMENTAL, default-off)

Spike for mctlhq/mctl-agents#746, scaffolded by #759 (slice 1). This crate only
prints its name and version. It is not built into the container image, not
imported by the orchestrator, excluded by `.dockerignore`, and guarded by
`tests/test_portable_runtime_isolation.py`.

## Build and run

    cd experiments/portable-runtime-rs
    cargo run --locked

CI verification lives in `.github/workflows/portable-runtime-spike.yml`
(optional, not a required check).

## Delete

    git rm -r experiments/portable-runtime-rs .github/workflows/portable-runtime-spike.yml

The isolation test then skips itself.
