"""Universal auth for the Claude Agent SDK.

Scenarios:
- Local dev / prototype: CLAUDE_CODE_OAUTH_TOKEN (Claude Pro/Max).
- Production / in-cluster deploy: ANTHROPIC_API_KEY (Console billing).

The same code path works for both modes — the choice is driven by which
env var is present.
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class AuthMode:
    name: str          # "oauth" or "api_key"
    env_var: str       # name of the env var holding the token/key
    description: str   # human-readable description


def detect_auth() -> AuthMode:
    """Detect the auth mode from env. OAuth wins when both are set."""
    oauth = os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()

    if oauth:
        if not oauth.startswith("sk-ant-oat01-"):
            print(
                "⚠️  CLAUDE_CODE_OAUTH_TOKEN does not look like an OAuth token "
                "(should start with sk-ant-oat01-). Did you paste an API key by mistake?"
            )
        return AuthMode(
            name="oauth",
            env_var="CLAUDE_CODE_OAUTH_TOKEN",
            description="Claude Pro/Max OAuth token (billed against the subscription)",
        )

    if api_key:
        if not api_key.startswith("sk-ant-api03-"):
            print(
                "⚠️  ANTHROPIC_API_KEY does not look like an API key "
                "(should start with sk-ant-api03-)."
            )
        return AuthMode(
            name="api_key",
            env_var="ANTHROPIC_API_KEY",
            description="Anthropic Console API key (pay-per-token)",
        )

    # Third path: no OAuth token and no API key in env, but the local
    # `claude` CLI is authenticated (~/.claude/.credentials.json or an
    # interactive session). The SDK picks up CLI auth via subprocess.
    # Convenient in dev; do not rely on it in production / cron / Docker
    # where the CLI is not logged in.
    import shutil
    if shutil.which("claude"):
        return AuthMode(
            name="cli_session",
            env_var="(claude CLI session)",
            description="existing authentication of the local `claude` CLI",
        )

    raise RuntimeError(
        "Auth not found: neither CLAUDE_CODE_OAUTH_TOKEN nor ANTHROPIC_API_KEY is set, "
        "and the `claude` CLI is not installed. Put a token in .env or install Claude Code."
    )


def ensure_auth_for_sdk() -> AuthMode:
    """Prepare env so the Claude Agent SDK picks up the right credential.

    The SDK reads both variables on its own — we only log the choice and
    clean up empty/garbage values to avoid priority conflicts.
    """
    mode = detect_auth()

    if mode.name == "oauth":
        # The CLI gives the API key precedence over OAuth — drop it if it's empty.
        if not os.getenv("ANTHROPIC_API_KEY", "").strip():
            os.environ.pop("ANTHROPIC_API_KEY", None)

    print(f"🔑 Auth mode: {mode.name} — {mode.description}")
    return mode


if __name__ == "__main__":
    ensure_auth_for_sdk()
