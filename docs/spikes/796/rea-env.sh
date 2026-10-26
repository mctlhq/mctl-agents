#!/bin/sh
# Run a command with common credential variables removed from its environment
# (issue #796). This is hygiene, NOT a security boundary: REA runs as the same
# uid as its parent processes and could still read their environment via
# procfs, and the job's GITHUB_ENV/GITHUB_PATH files remain reachable.
exec env -u CLAUDE_CODE_OAUTH_TOKEN -u GITHUB_TOKEN -u GH_TOKEN \
  -u ACTIONS_RUNTIME_TOKEN -u ACTIONS_ID_TOKEN_REQUEST_TOKEN \
  -u ACTIONS_ID_TOKEN_REQUEST_URL -u GITHUB_ENV -u GITHUB_PATH \
  -u GITHUB_OUTPUT "$@"
