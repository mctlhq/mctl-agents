FROM python:3.12-slim

WORKDIR /app

# Claude Agent SDK spawns the `claude` CLI subprocess — install it.
# Using a slim runtime; npm is required for the install.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        nodejs \
        npm \
        git \
        openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && npm install -g @anthropic-ai/claude-code@latest

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
