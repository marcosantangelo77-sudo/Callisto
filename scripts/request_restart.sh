#!/usr/bin/env bash
# Request a Callisto restart via signal file.
# Sentinel picks this up within 30 seconds and restarts the API with new code.
# Usage: bash scripts/request_restart.sh "reason for restart"

SIGNAL_FILE="$(dirname "$0")/../memory/restart_requested"
REASON="${1:-code reload}"

echo "$REASON" > "$SIGNAL_FILE"
echo "Restart signal written. Sentinel will pick it up within 30 seconds."
echo "Reason: $REASON"
