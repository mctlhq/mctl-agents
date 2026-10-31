#!/bin/sh
# entrypoint.sh — exec the given command.
#
# This script must not write anywhere under /app: the image runs with
# readOnlyRootFilesystem (mctlhq/mctl-gitops#2290). Runtime state
# (inbox/, proposals/, _mentor/digest/) is resolved from STATE_DIR in Python
# (config.settings.agent_state_root) and falls back to the baked-in agents/
# paths when STATE_DIR is unset.

set -e

exec "$@"
