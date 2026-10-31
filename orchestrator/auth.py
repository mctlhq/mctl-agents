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

from orchestrator.credential_files import read_credential

load_dotenv()


@dataclass
class AuthMode:
    name: str          # "oauth" or "api_key"
    env_var: str       # name of the env var holding the token/key
    description: str   # human-readable description


# Set by rotate_to_secondary_auth(): the next sessions use the secondary
# credential. Held here, never written to os.environ.
_use_secondary = False

_OAUTH = "CLAUDE_CODE_OAUTH_TOKEN"
_API_KEY = "ANTHROPIC_API_KEY"


def _active_credential(primary: str) -> tuple[str, str]:
    """(variable the credential came from, value) for `primary` or its
    `_SECONDARY`; ("", "") when neither resolves. Each is read from
    `<NAME>_FILE` when set, else from the env (credential_files)."""
    secondary = f"{primary}_SECONDARY"
    if _use_secondary:
        value = read_credential(secondary)
        if value:
            # Reported under the primary name, as before: rotation used to
            # copy the secondary value into the primary variable.
            return primary, value
    value = read_credential(primary)
    if value:
        return primary, value
    value = read_credential(secondary)
    if value:
        return secondary, value
    return "", ""


def detect_auth() -> AuthMode:
    """Detect the auth mode from env. OAuth wins when both are set."""
    oauth_var, oauth = _active_credential(_OAUTH)
    api_var, api_key = _active_credential(_API_KEY)

    if oauth:
        active_var = oauth_var
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
        active_var = api_var
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


def session_auth_env() -> dict[str, str]:
    """The single active Anthropic credential, under the variable the bundled
    CLI reads, for one session's `options.env`. Never written to os.environ.

    The CLI child needs it in its own env, so model Bash can read it too
    (credential_files.RESIDUAL_BOUNDARY, mctlhq/mctl-agents#869).
    """
    try:
        mode = detect_auth()
    except RuntimeError:
        return {}
    if mode.name == "oauth":
        return {_OAUTH: _active_credential(_OAUTH)[1]}
    if mode.name == "api_key":
        return {_API_KEY: _active_credential(_API_KEY)[1]}
    return {}


def rotate_to_secondary_auth() -> bool:
    """Switch the next sessions to the secondary credentials, if available,
    upon 429 RateLimit. Reads the secondary from its file when file-backed;
    does not copy it into os.environ (see session_auth_env)."""
    global _use_secondary
    rotated = False
    for primary in (_OAUTH, _API_KEY):
        secondary = f"{primary}_SECONDARY"
        if read_credential(secondary):
            print(f"info: rotated {primary} to {secondary}")
            rotated = True
    if rotated:
        _use_secondary = True
    return rotated


def ensure_auth_for_sdk() -> AuthMode:
    """Prepare env so the Claude Agent SDK picks up the right credential.

    The SDK reads both variables on its own — we only log the choice and
    clean up empty/garbage values to avoid priority conflicts.
    """
    mode = detect_auth()

    if mode.name == "oauth":
        # The CLI gives the API key precedence over OAuth — drop it if it's empty.
        if not read_credential(_API_KEY):
            os.environ.pop("ANTHROPIC_API_KEY", None)

    print(f"info: auth mode: {mode.name} — {mode.description}")
    return mode


if __name__ == "__main__":
    ensure_auth_for_sdk()
