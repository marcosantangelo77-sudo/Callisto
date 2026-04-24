#!/usr/bin/env bash
# Convenience wrapper: run the local-only E2E from a shell.
# See scripts/local_only_e2e.py for the full description.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$ROOT"
exec python scripts/local_only_e2e.py "$@"
