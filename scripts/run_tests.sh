#!/usr/bin/env bash
# CI-lite test runner: fail fast, short traceback, clear status line.
# Uses isolated temp DBs — NEVER touches callisto.db.
set -u

cd "$(dirname "$0")/.."

GREEN='\033[0;32m'
RED='\033[0;31m'
BOLD='\033[1m'
NC='\033[0m'

PYTHON="${PYTHON:-python}"

echo "Running pytest -x --tb=short ..."
if "$PYTHON" -m pytest tests/ -x --tb=short "$@"; then
    printf "${GREEN}${BOLD}[OK]${NC} all tests passed\n"
    exit 0
else
    rc=$?
    printf "${RED}${BOLD}[FAIL]${NC} pytest exited with code %s\n" "$rc"
    exit "$rc"
fi
