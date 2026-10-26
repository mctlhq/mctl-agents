# SecurityFinding v1 contract fixtures

Captured from mctl-api at ref `4.124.0` (tag contains `8023be018`, PR #799):

- `docs/security-findings.md` (whole file; lifecycle, strength order, error codes, idempotency).
- `internal/openapi/openapi.yaml`:
  - lines 1444-1700: schemas `SecurityFindingError` ... `SecurityDismissRequest` (`schemas/*.json`);
  - lines 5950-6239: the `/api/v1/security/*` paths (`routes.json`).

`vocabulary.json` merges the enums of those schemas with the strength order and default minimum from
`docs/security-findings.md`. The error enum at this ref also holds `work_items_unavailable` and
`internal_error`, which the issue text does not list. `orchestrator/security_findings/vocabulary.json` is a
byte-identical copy (the container image does not ship `tests/`); a test pins the two together.

The `example-*.json` files are hand-written answers shaped by the schemas, one per answer branch the client
distinguishes. They are not captured traffic.
