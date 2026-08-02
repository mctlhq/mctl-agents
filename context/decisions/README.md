# Architecture Decision Records

This directory holds ADRs for the `mctl-agents` repository — the platform's
agent orchestrator. Records are written in Markdown, numbered sequentially,
and stored under `context/decisions/`.

These ADRs are **internal** to this repository. Their purpose is to capture
the context behind decisions about the orchestrator (language and runtime
choice, harness dependencies, tooling) so future maintainers don't need to
reverse engineer past decisions from git history.

Numbering is per-repository, not shared across the organisation. The
equivalent directory in `mctl-docs` is scoped to that repository's own
stack and is not a cross-repository registry.

## Format

Each ADR file follows the lightweight Nygard template:

- **Status** — proposed / accepted / superseded / deprecated
- **Date** — ISO date the decision was recorded
- **Context** — what forced this decision
- **Decision** — what we chose
- **Consequences** — trade-offs and follow-ups

Two additional sections are used where they add value:

- **Drivers** — the reasoning behind the decision, one numbered item each
- **Revisit criteria** — the measurable conditions under which the decision
  should be reconsidered. A decision without them tends to be read as
  permanent, which is rarely what was meant.

Keep ADRs durable: version numbers, line references, and benchmark figures
go stale quickly and belong in an issue or a linked research note, not in
the record itself.

Filenames use the pattern `NNNN-short-slug.md` where `NNNN` is a
zero-padded sequence number.

## Index

| ID | Title | Status |
|----|-------|--------|
| 0001 | [Keep mctl-agents on Python](./0001-keep-mctl-agents-on-python.md) | proposed |
