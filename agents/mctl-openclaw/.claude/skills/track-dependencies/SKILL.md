---
name: track-dependencies
description: Use when checking for npm / Python dependency updates for mctl-openclaw.
---

# Track dependencies

When you need to see what is new in the service's dependencies:

1. The list of key dependencies lives in `context/architecture.md` (section "Dependencies").
2. For each dependency, check `https://github.com/<owner>/<repo>/releases` via WebFetch.
3. Compare with the current version recorded in `context/current-version.md`.
4. Only releases that are **above** our current version **and** ship a
   security fix, a meaningful performance improvement, or a breaking
   change are worth recording.

Do not duplicate anything already captured under `context/decisions/` as
a deliberately deferred upgrade.
