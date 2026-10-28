# REA upstream snapshot (issue #796)

| Field | Value |
| --- | --- |
| Repository | https://github.com/morluto/rea |
| Commit SHA | `160d821ac9a0310afcb8d1fc427862dd6508add5` (`refs/tags/rea-agents-6.8.0`) |
| Package / release version | `rea-agents` 6.8.0 (npm), `dist.integrity` `sha512-9QDeRnGqEDzABZyr04eLnBXBv/c6I9/TWu/6M0MD4pEfkySI/BIgwTODIbAReX3C1x6hjw9+Jlx4A2yUSPZ9ww==` |
| Install method | `npm install --ignore-scripts --prefix <tmp> rea-agents@6.8.0`, bound to the SHA by npm SLSA provenance (`npm_provenance_git_commit` = SHA) and `npm audit signatures`; npm records no `gitHead` for this OIDC-published package |
| Entrypoints | CLI `rea`, MCP `rea mcp`, both through `docs/spikes/796/rea-env.sh` (removed with the harness, #850) |
| Test date | 2026-10-27 (runs 40525903080, 40529056591, 40533093166; Qwen row on a workstation container the same day) |

Local dependencies, only for the fixtures used:

- `native-codec`: `cc` (GCC 13.3.0), GNU Make 4.3, GNU strip 2.42, `sha256sum`.
- `packaged-app`: JDK 17 (`javac`, `jar` 17.0.20.1). No Android SDK was used; the artefact is the JVM jar fallback.

## How to run

The `rea-spike-796` workflow, `tools/rea_spike.py` and `rea-env.sh` were
removed after the verdict (`REJECT`, mctlhq/.github#268; #850). They are at
commit `3ea66bd` and earlier. To re-open the question, restore them from there
and dispatch the workflow as before: it takes the inputs `rea_ref`, `model` and
`probe_arm`, and writes nothing to the repository. The fixtures, oracles, stage
prompts and results under `docs/spikes/796/` are kept as the record.
