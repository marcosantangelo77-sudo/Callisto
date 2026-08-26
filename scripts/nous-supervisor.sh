#!/usr/bin/env bash
# Launch ONE Ox Alpha worker (Hermes CLI → Nous Portal stealth/ox-alpha).
#
# Usage:
#   bash scripts/nous-supervisor.sh <task-name> <worktree> <prompt-file> [idle-minutes]
#
# Contract (ORCHESTRATION_HANDOFF.md):
#   * Foreground. The caller owns the PTY. Do not nohup this script.
#   * Hermes argv includes --provider nous -m stealth/ox-alpha
#   * At most 3 concurrent Hermes processes on the host (CALLISTO_HERMES_MAX_PROCS)
#   * Refuse to start if Nous Portal is not logged in
#   * Refuse to start against master / a missing worktree / a missing prompt
#   * Do not print, copy, or log credentials
#
# ChatGPT's workstation used ~/callisto-wt/nous-supervisor.sh (not in this
# repo). This file is the in-tree equivalent so a cloud runner and the
# workstation share one launcher.
set -euo pipefail

TASK_NAME="${1:-}"
WORKTREE="${2:-}"
PROMPT_FILE="${3:-}"
IDLE_MINUTES="${4:-180}"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
export PATH="${HOME}/.local/bin:${HOME}/.hermes/bin:${PATH}"

die() { echo "nous-supervisor: $*" >&2; exit 2; }

[[ -n "$TASK_NAME" && -n "$WORKTREE" && -n "$PROMPT_FILE" ]] \
  || die "usage: $0 <task-name> <worktree> <prompt-file> [idle-minutes]"

[[ -d "$WORKTREE" ]] || die "worktree does not exist: $WORKTREE"
[[ -f "$PROMPT_FILE" ]] || die "prompt file missing: $PROMPT_FILE"

# Refuse to run against the primary checkout's master working tree by name.
branch="$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
[[ "$branch" != "master" && "$branch" != "main" ]] \
  || die "refusing to launch an OX worker on $branch (use a dedicated worktree/branch)"

command -v hermes >/dev/null || [[ -x "${HOME}/.hermes/bin/hermes" ]] \
  || die "hermes binary not found"

MAX_PROCS="${CALLISTO_HERMES_MAX_PROCS:-3}"
# Count live hermes processes owned by this user. Broad pkill is forbidden
# by the handoff; this is a preflight cap, not a killer.
running="$(pgrep -u "$(id -u)" -c -f '(^|/)hermes( |$)' 2>/dev/null || true)"
running="${running:-0}"
if [[ "$running" -ge "$MAX_PROCS" ]]; then
  die "already ${running} hermes processes (cap ${MAX_PROCS}). wait for a slot."
fi

# Honest login check — python, no token print.
if ! python3 "${ROOT}/scripts/oxa_status.py" >/tmp/oxa_status_${TASK_NAME}.txt; then
  cat /tmp/oxa_status_${TASK_NAME}.txt >&2 || true
  die "Nous Portal is not logged in. Run: hermes auth add nous --type oauth --no-browser"
fi

LOG_DIR="${CALLISTO_OXA_LOG_DIR:-${ROOT}/logs/oxa}"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/${TASK_NAME}.log"
PROMPT="$(cat "$PROMPT_FILE")"

echo "nous-supervisor: task=${TASK_NAME} worktree=${WORKTREE} branch=${branch}"
echo "nous-supervisor: model=stealth/ox-alpha provider=nous idle_minutes=${IDLE_MINUTES}"
echo "nous-supervisor: log=${LOG}"

# Idle window is advisory: Hermes -z is one-shot. Do not `set -x` — the
# prompt must not land in the shell trace. Do not --ignore-user-config
# (that drops Portal auth). Pin --in so the worker cannot wander to master.
hermes \
  --provider nous \
  -m stealth/ox-alpha \
  --in "$WORKTREE" \
  -z "$PROMPT" \
  2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
