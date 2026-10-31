"""Keep GITHUB_TOKEN fresh across long-running orchestrator processes.

`implement`/`investigate`/`shepherd` run inside a single pod for up to
activeDeadlineSeconds=7200 (2h), but a GitHub App installation token lives
~60min. Argo mounts the Vault-rotated token as a Secret volume — kubelet
refreshes that file's content roughly every sync period — but a value read
into os.environ at process start stays frozen for the process's whole
lifetime; a plain `env: GITHUB_TOKEN: valueFrom: secretKeyRef` only ever
sees the token as of pod start.

`_run()` in run_implementer.py / run_shepherd.py / run_issue_investigator.py
calls subprocess.run() without an explicit `env=`, so every `gh`/`git`
subprocess inherits whatever is in os.environ at the moment it's spawned —
refreshing os.environ["GITHUB_TOKEN"] right before each such call is
sufficient; no per-subprocess plumbing needed.
"""
from __future__ import annotations

import os

from orchestrator.credential_files import read_credential


def refresh_github_token() -> None:
    """Refresh os.environ["GITHUB_TOKEN"] from GITHUB_TOKEN_FILE, if configured.

    No-op when GITHUB_TOKEN_FILE is unset and no GITHUB_TOKEN env seed exists.
    When the file is momentarily unreadable (a mid-rotation race, possibly
    invalid UTF-8) `read_credential` falls back to the last good value, then
    to the env seed; the existing os.environ value is never cleared. Only the
    variable name, path and exception type name are logged: a
    UnicodeDecodeError message embeds a byte of the token.
    """
    token = read_credential("GITHUB_TOKEN")
    if token:
        os.environ["GITHUB_TOKEN"] = token
