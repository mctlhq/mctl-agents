FROM python:3.14-slim

WORKDIR /app

# The harness CLI version is pinned by claude-agent-sdk in uv.lock, not here.
# The SDK bundles its own Claude Code CLI binary (claude_agent_sdk/_bundled/)
# and prefers it over anything on PATH — see _find_cli() in
# claude_agent_sdk/_internal/transport/subprocess_cli.py. A separate global
# `npm install -g @anthropic-ai/claude-code` used to live here; it installed a
# second CLI that the SDK never actually ran, so the ARG that pinned its
# version controlled nothing. Bump the harness by bumping claude-agent-sdk in
# pyproject.toml, deliberately, in an explicit commit (see issue #44).
#
# nodejs/npm stay: they are not CLI-install plumbing, they are tools the
# implementer agent needs directly. 6 of the 11 services in SERVICES
# (config/settings.py) are Node repositories, and
# agents/mctl-web/.claude/agents/implementer.md tells the agent to run
# `npm install --no-save && npm run lint` as its sanity check.
# `gh` CLI is required by the Tier 2 implementer (orchestrator/run_implementer.py)
# for `gh repo clone mctlhq/<svc>` + `gh pr create`. Installed from the
# official cli.github.com Debian repo.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        nodejs \
        npm \
        git \
        openssh-client \
    && curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg \
         -o /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && chmod go+r /usr/share/keyrings/githubcli-archive-keyring.gpg \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/usr/share/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" \
         > /etc/apt/sources.list.d/github-cli.list \
    && apt-get update && apt-get install -y --no-install-recommends gh \
    && rm -rf /var/lib/apt/lists/*

# uv, pinned by digest like everything else here: an unpinned installer is the
# same class of drift the lockfile exists to prevent, and a registry tag can be
# moved after the fact in a way uv.lock hashes cannot. The digest resolves to a
# multi-arch index (linux/amd64 + linux/arm64), so this does not tie the build
# to one architecture. Keep the tag comment in step when bumping.
COPY --from=ghcr.io/astral-sh/uv@sha256:798712e57f879c5393777cbda2bb309b29fcdeb0532129d4b1c3125c5385975a /uv /usr/local/bin/uv
# ^ ghcr.io/astral-sh/uv:0.11.11

COPY pyproject.toml uv.lock ./
# --locked, not --frozen: --frozen only fails if uv.lock is missing entirely
#   and otherwise installs whatever it already pins, even if pyproject.toml
#   has since moved — it would build a stale tree in silence. --locked
#   rejects that mismatch, which is the actual point of committing the lock.
# --no-dev: `dev` is a default group in uv, so without this the production
#   image ships pytest (and later ruff and mypy).
RUN uv sync --locked --no-dev --no-cache

# Put the locked environment on PATH rather than invoking `uv run`, which
# re-checks the environment on each call and can pull the dev group back in.
# This also means `python` is the locked interpreter for anything the agents
# run through their Bash tool.
ENV PATH="/app/.venv/bin:$PATH"

# Code + per-service CLAUDE.md, .claude/, context/ are baked in.
# inbox/ proposals/ digest/ live in mctl-gitops/agents-state/ and are linked
# in by the entrypoint wrapper at runtime.
COPY orchestrator/ ./orchestrator/
COPY config/ ./config/
COPY agents/ ./agents/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Non-root runtime (SOC F8). uid 1000 matches the worker Helm
# podSecurityContext and the CWFT clone-gitops `chown -R 1000:1000 /workdir`
# handoff so STATE_DIR/TMPDIR stay writable after the alpine/git clone step.
RUN groupadd --gid 1000 app \
    && useradd --uid 1000 --gid app --create-home --home-dir /home/app app \
    && chown -R app:app /app /home/app
ENV HOME=/home/app
USER app:app

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "orchestrator.run_all"]
