# Contributing to mctl-agents

Thank you for your interest in contributing to mctl-agents! This guide will help you get started.

## Prerequisites

- **Python 3.12+**
- **[uv](https://docs.astral.sh/uv/getting-started/installation/)** — the
  version pinned in CI and the Dockerfile is `0.11.11`; any recent uv works
  for local development.
- **Docker** (for container builds)

## Local Development

Install dependencies:

```bash
uv sync
```

This installs exactly what `uv.lock` pins, including the `dev` group (pytest).
The image builds with `--no-dev` so those tools stay out of production.

Adding or changing a dependency means editing `pyproject.toml` and running
`uv lock`; commit the updated lockfile alongside it. Do not install into the
environment ad hoc — an unpinned harness lets an image rebuild silently change
agent behaviour, which is the whole reason the lockfile is committed.

The orchestrator entry points live in `orchestrator/` (see `README.md` for the
agent/mentor run flow). Runs require either a Claude OAuth token or an API key —
see `orchestrator/auth.py`.

## Testing

```bash
uv run pytest tests/
```

Please ensure tests pass before submitting a pull request.

## Pull Request Process

1. Create a feature branch (`feat/...`, `fix/...`, `chore/...`) — never commit to `main` directly
2. Use [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `chore:`, ...)
3. Open a pull request; CI and an automated review must pass before merge

## Code Style

- English for all code, comments, and documentation
- No emoji in code or commit messages

## Reporting Issues

Open a GitHub issue with a clear description and reproduction steps where applicable.

## Security Issues

Please do NOT open public issues for security vulnerabilities — see [SECURITY.md](SECURITY.md).
