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
