# REA upstream snapshot (issue #796)

| Field | Value |
| --- | --- |
| Repository | https://github.com/morluto/rea |
| Commit SHA | `160d821ac9a0310afcb8d1fc427862dd6508add5` (`refs/tags/rea-agents-6.8.0`) |
| Package / release version | `rea-agents` 6.8.0 (npm), `dist.integrity` `sha512-9QDeRnGqEDzABZyr04eLnBXBv/c6I9/TWu/6M0MD4pEfkySI/BIgwTODIbAReX3C1x6hjw9+Jlx4A2yUSPZ9ww==` |
| Install method | `npm install --ignore-scripts --prefix <tmp> rea-agents@6.8.0`, bound to the SHA by npm SLSA provenance (`npm_provenance_git_commit` = SHA) and `npm audit signatures`; npm records no `gitHead` for this OIDC-published package |
| Entrypoints | CLI `rea`, MCP `rea mcp`, both through `docs/spikes/796/rea-env.sh` |
| Test date | 2026-10-27 (runs 40525903080, 40529056591, 40533093166; Qwen row on a workstation container the same day) |

Local dependencies, only for the fixtures used:

- `native-codec`: `cc` (GCC 13.3.0), GNU Make 4.3, GNU strip 2.42, `sha256sum`.
- `packaged-app`: JDK 17 (`javac`, `jar` 17.0.20.1). No Android SDK was used; the artefact is the JVM jar fallback.

## How to run

Dispatch the `rea-spike-796` workflow (Actions tab, `workflow_dispatch` only)
from `main`. Inputs: `rea_ref` (required; branch, tag or commit of
`morluto/rea`, resolved and recorded as a full SHA), `model` (default
`claude-opus-5-5`) and `probe_arm` (`rea-mcp` or `rea-cli`). The run uploads
the artifact `rea-spike-796-<run_id>` (14 days) with records, report, REA and
fixture metadata, schema bytes, stage probes and an empty grading template.
The workflow writes nothing to the repository: results reach `main` only
through a separate, reviewed PR that updates the report and this file.
