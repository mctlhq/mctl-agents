# Execution-provenance vectors

The review gate (ADR 025) uses `attempt_id` and `lineage_root`, which mctl-api
derives under the `execution_attempt_v1` contract
(mctl-api `docs/execution-provenance.md`).

mctl-agents vendors the canonical vectors byte for byte at
`tests/fixtures/execution_provenance/execution_attempt_vectors.json`
(SHA-256 `9ab9cf04a5542004f5e851a0f32b87681249d3e6f7027386945ebe76a46ad24c`).
`tests/test_execution_provenance_vectors.py` pins that digest and recomputes
every vector with `orchestrator/execution_provenance.py`.

Never edit the file. To change it, re-vendor from mctl-api and update `PINNED`
in the test in the same commit.
