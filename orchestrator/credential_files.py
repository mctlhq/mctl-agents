"""File-backed credential readers (mctl-agents#807 G2, runtime side).

Every agent credential is read from `<NAME>_FILE` (a mounted Secret volume)
at the credentialed operation, so a rotation takes effect without restarting
a 2h pod. `<NAME>` in the environment stays a transitional fallback, used when
`<NAME>_FILE` is unset or has never been readable.

`read_credential()` never writes `os.environ`. In-process-only credentials
(mctl-api tokens, writer tokens, the scout bot token) therefore never reach
the env that SDK sessions, `gh`/`git` children and the model's `Bash` inherit.

Residual boundary: see `RESIDUAL_BOUNDARY`.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

RESIDUAL_BOUNDARY = (
    "A credential file in a filesystem readable by model Bash, and any credential "
    "a CLI child needs in its env, is obtainable by the model shell; "
    "mctlhq/mctl-agents#869 owns that boundary."
)

IN_PROCESS = "in-process"
SUBPROCESS_ENV = "subprocess-env"
CLI_ENV = "cli-env"

_LONG_LIVED = "long-lived, operator-rotated (exact lifetime: not observed)"


@dataclass(frozen=True)
class CredentialSpec:
    name: str       # e.g. "MCTL_USAGE_WRITER_TOKEN"
    klass: str      # IN_PROCESS | SUBPROCESS_ENV | CLI_ENV
    lifetime: str   # lifetime class; never a value

    @property
    def file_name(self) -> str:
        return f"{self.name}_FILE"


INVENTORY: tuple[CredentialSpec, ...] = (
    CredentialSpec("MCTL_TOKEN", IN_PROCESS, _LONG_LIVED),
    CredentialSpec("MCTL_USAGE_WRITER_TOKEN", IN_PROCESS, _LONG_LIVED),
    CredentialSpec("MCTL_EVIDENCE_WRITER_TOKEN", IN_PROCESS, _LONG_LIVED),
    CredentialSpec("SCOUT_TELEGRAM_BOT_TOKEN", IN_PROCESS, _LONG_LIVED),
    CredentialSpec("GITHUB_TOKEN", SUBPROCESS_ENV, "short-lived (GitHub App token, kubelet-refreshed file)"),
    CredentialSpec("CLAUDE_CODE_OAUTH_TOKEN", CLI_ENV, _LONG_LIVED),
    CredentialSpec("CLAUDE_CODE_OAUTH_TOKEN_SECONDARY", CLI_ENV, _LONG_LIVED),
    CredentialSpec("ANTHROPIC_API_KEY", CLI_ENV, _LONG_LIVED),
    CredentialSpec("ANTHROPIC_API_KEY_SECONDARY", CLI_ENV, _LONG_LIVED),
)

# Last good value read from each credential's file, in this process only.
_LAST_GOOD: dict[tuple[str, str], str] = {}


def file_env_names() -> frozenset[str]:
    """`<NAME>_FILE` of every in-process-only credential."""
    return frozenset(s.file_name for s in INVENTORY if s.klass == IN_PROCESS)


def read_credential(name: str, env: Mapping[str, str] | None = None) -> str:
    """Value of credential `name` for the operation about to run.

    `<name>_FILE` set: read it now and strip. On OSError, invalid UTF-8 or an
    empty file, fall back to the last good value read from that file in this
    process, else to env `<name>`, else "". Never writes `os.environ`; logs
    only the name, the path and the exception type name.
    """
    source = os.environ if env is None else env
    path = source.get(f"{name}_FILE", "").strip()
    if path:
        try:
            value = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, ValueError) as exc:
            logger.warning(
                "credential %s: could not read %s (%s); using fallback", name, path, type(exc).__name__
            )
        else:
            if value:
                _LAST_GOOD[(name, path)] = value
                return value
            logger.warning("credential %s: %s is empty; using fallback", name, path)
        if (name, path) in _LAST_GOOD:
            return _LAST_GOOD[(name, path)]
    return source.get(name, "").strip()
