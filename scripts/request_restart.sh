#!/usr/bin/env bash
# Request a Callisto restart via signal file.
# The API's restart_signal_watcher AND the watchdog both poll this file and
# will restart the process within ~15 seconds.
#
# Signal lives off OneDrive (oplocks there can freeze the watchdog).
# Resolution mirrors tools/state_paths.py:
#   1. $CALLISTO_STATE_DIR  (explicit override)
#   2. $LOCALAPPDATA/Callisto (Windows / Git Bash on Windows)
#   3. ~/.local/state/callisto (Unix fallback)
#
# Usage: bash scripts/request_restart.sh "reason for restart"

set -eu

REASON="${1:-code reload}"

if [ -n "${CALLISTO_STATE_DIR:-}" ]; then
    STATE_DIR="$CALLISTO_STATE_DIR"
elif [ -n "${LOCALAPPDATA:-}" ]; then
    STATE_DIR="$LOCALAPPDATA/Callisto"
else
    STATE_DIR="$HOME/.local/state/callisto"
fi

mkdir -p "$STATE_DIR"
SIGNAL_FILE="$STATE_DIR/restart_requested"

printf '%s\n' "$REASON" > "$SIGNAL_FILE"
echo "Restart signal written to: $SIGNAL_FILE"
echo "API and watchdog both poll this file; restart within ~15s."
echo "Reason: $REASON"
