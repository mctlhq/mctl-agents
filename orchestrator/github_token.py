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
from pathlib import Path


def refresh_github_token() -> None:
    """Re-read GITHUB_TOKEN from GITHUB_TOKEN_FILE, if configured.

    No-op when GITHUB_TOKEN_FILE is unset (short-lived crons like
    issue-poll/reconcile keep using the static secretKeyRef env var — their
    whole run comfortably fits inside one token's TTL) or when the file is
    momentarily unreadable (a mid-rotation race). In both cases the existing
    os.environ["GITHUB_TOKEN"] — set at pod start — is left as-is rather than
    cleared, so callers never regress to no-token behaviour.
    """
    path = os.environ.get("GITHUB_TOKEN_FILE", "").strip()
    if not path:
        return
    try:
        token = Path(path).read_text().strip()
    except (OSError, ValueError) as e:
        # ValueError covers UnicodeDecodeError: a Secret volume update isn't
        # atomic from the reader's side, so a read landing mid-rotation can
        # observe a truncated write with invalid UTF-8 tail bytes. That must
        # degrade the same as a missing file, not crash — an uncaught
        # exception here would propagate out of _run() and abort every
        # subsequent gh/git call for the rest of the process.
        print(f"refresh_github_token: could not read {path}: {e}; keeping previous token")
        return
    if token:
        os.environ["GITHUB_TOKEN"] = token
