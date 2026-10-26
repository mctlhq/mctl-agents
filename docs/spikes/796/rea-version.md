# REA upstream snapshot (issue #796)

STATUS: DRAFT -- to be filled by the operator at the start of the live run.

| Field | Value |
| --- | --- |
| Repository | https://github.com/morluto/rea |
| Commit SHA | unobserved (operator: record the exact SHA before any run) |
| Package / release version | unobserved |
| Install method | unobserved |
| Test date | unobserved |

Local dependencies are listed only for fixtures actually used:

- `native-codec`: a C compiler (`cc`), `make`, `strip`, `sha256sum`.
- `packaged-app`: a JDK (`javac`, `jar`). An Android SDK/Gradle APK build is
  used instead only if that toolchain is present; the report must say which.

## How to run

Dispatch the `rea-spike-796` workflow (Actions tab, `workflow_dispatch` only)
from `main`. Inputs: `rea_ref` (required; branch, tag or commit of
`morluto/rea`, resolved and recorded as a full SHA), `model` (default
`claude-opus-5-5`) and `probe_arm` (`rea-mcp` or `rea-cli`). The run uploads
the artifact `rea-spike-796-<run_id>` (14 days) with records, report, REA and
fixture metadata, schema bytes, stage probes and an empty grading template.
The workflow writes nothing to the repository: results reach `main` only
through a separate, reviewed PR that updates the report and this file.
