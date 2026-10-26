#!/bin/sh
# Run a command with credentials stripped from its environment (issue #796).
# Used to launch REA (MCP server and CLI) so it never sees the Claude or
# GitHub tokens present in the job environment.
exec env -u CLAUDE_CODE_OAUTH_TOKEN -u GITHUB_TOKEN -u GH_TOKEN \
  -u ACTIONS_RUNTIME_TOKEN -u ACTIONS_ID_TOKEN_REQUEST_TOKEN \
  -u ACTIONS_ID_TOKEN_REQUEST_URL "$@"
