FROM python:3.12-slim

# Pinned harness CLI — the Claude Agent SDK spawns this exact `claude` binary.
# Bump deliberately, in an explicit commit (see issue #44).
ARG CLAUDE_CODE_VERSION=2.1.198

WORKDIR /app

# Claude Agent SDK spawns the `claude` CLI subprocess — install it.
# Using a slim runtime; npm is required for the install.
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
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}"

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Code + per-service CLAUDE.md, .claude/, context/ are baked in.
# inbox/ proposals/ digest/ live in mctl-gitops/agents-state/ and are linked
# in by the entrypoint wrapper at runtime.
COPY orchestrator/ ./orchestrator/
COPY config/ ./config/
COPY agents/ ./agents/
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
CMD ["python", "-m", "orchestrator.run_all"]
