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
    oauth = (
        os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        or os.getenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "").strip()
    )
    api_key = (
        os.getenv("ANTHROPIC_API_KEY", "").strip()
        or os.getenv("ANTHROPIC_API_KEY_SECONDARY", "").strip()
    )

    if oauth:
        active_var = (
            "CLAUDE_CODE_OAUTH_TOKEN"
            if os.getenv("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
            else "CLAUDE_CODE_OAUTH_TOKEN_SECONDARY"
        )
        if not oauth.startswith("sk-ant-oat01-"):
            print(
                f"warn: {active_var} does not look like an OAuth token "
                "(should start with sk-ant-oat01-). Did you paste an API key by mistake?"
            )
        return AuthMode(
            name="oauth",
            env_var=active_var,
            description=f"Claude Pro/Max OAuth token ({active_var})",
        )

    if api_key:
        active_var = (
            "ANTHROPIC_API_KEY"
            if os.getenv("ANTHROPIC_API_KEY", "").strip()
            else "ANTHROPIC_API_KEY_SECONDARY"
        )
        if not api_key.startswith("sk-ant-api03-"):
            print(
                f"warn: {active_var} does not look like an API key "
                "(should start with sk-ant-api03-)."
            )
        return AuthMode(
            name="api_key",
            env_var=active_var,
            description=f"Anthropic Console API key ({active_var})",
        )

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


def rotate_to_secondary_auth() -> bool:
    """Switch primary credentials to secondary if available upon 429 RateLimit."""
    rotated = False
    if os.getenv("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", "").strip():
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = os.environ["CLAUDE_CODE_OAUTH_TOKEN_SECONDARY"].strip()
        print("info: rotated CLAUDE_CODE_OAUTH_TOKEN to CLAUDE_CODE_OAUTH_TOKEN_SECONDARY")
        rotated = True

    if os.getenv("ANTHROPIC_API_KEY_SECONDARY", "").strip():
        os.environ["ANTHROPIC_API_KEY"] = os.environ["ANTHROPIC_API_KEY_SECONDARY"].strip()
        print("info: rotated ANTHROPIC_API_KEY to ANTHROPIC_API_KEY_SECONDARY")
        rotated = True

    return rotated


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

    print(f"info: auth mode: {mode.name} — {mode.description}")
    return mode


if __name__ == "__main__":
    ensure_auth_for_sdk()
