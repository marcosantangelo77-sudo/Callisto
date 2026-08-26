#!/usr/bin/env bash
# Launch ONE Ox Alpha worker (Hermes CLI → OpenRouter or Nous Portal).
#
# Usage:
#   bash scripts/nous-supervisor.sh <task-name> <worktree> <prompt-file> [idle-minutes]
#
# Provider:
#   CALLISTO_HERMES_PROVIDER=openrouter|nous
#   Default: openrouter when OPENROUTER_API_KEY is set (env or ~/.hermes/.env),
#   else nous. OpenRouter HTTP for stealth/ox-alpha is the fast path
#   (~4s vs ~12s Nous CLI fork). Never print the key.
#
# Contract:
#   * Foreground. The caller owns the PTY. Do not nohup this script.
#   * Hermes argv includes --provider <openrouter|nous> -m stealth/ox-alpha
#   * At most CALLISTO_HERMES_MAX_PROCS concurrent Hermes processes
#   * Refuse to start against master / a missing worktree / a missing prompt
#   * Do not print, copy, or log credentials
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

# Load OPENROUTER_API_KEY from ~/.hermes/.env without printing it.
if [[ -z "${OPENROUTER_API_KEY:-}" && -f "${HOME}/.hermes/.env" ]]; then
  _or_key="$(python3 - <<'PY'
from pathlib import Path
p = Path.home() / ".hermes" / ".env"
for line in p.read_text(encoding="utf-8").splitlines():
    s = line.strip()
    if not s or s.startswith("#") or "=" not in s:
        continue
    k, v = s.split("=", 1)
    if k.strip() == "OPENROUTER_API_KEY":
        v = v.strip().strip('"').strip("'")
        if v:
            print(v)
            break
PY
)"
  if [[ -n "${_or_key}" ]]; then
    export OPENROUTER_API_KEY="${_or_key}"
  fi
  unset _or_key
fi

PROVIDER="${CALLISTO_HERMES_PROVIDER:-}"
if [[ -z "${PROVIDER}" ]]; then
  if [[ -n "${OPENROUTER_API_KEY:-}" ]]; then
    PROVIDER="openrouter"
  else
    PROVIDER="nous"
  fi
fi
MODEL="${CALLISTO_HERMES_MODEL:-stealth/ox-alpha}"

if [[ "${PROVIDER}" == "nous" ]]; then
  # Honest login check — python, no token print.
  if ! python3 "${ROOT}/scripts/oxa_status.py" >/tmp/oxa_status_${TASK_NAME}.txt; then
    cat /tmp/oxa_status_${TASK_NAME}.txt >&2 || true
    die "Nous Portal is not logged in. Run: hermes auth add nous --type oauth --no-browser"
  fi
elif [[ "${PROVIDER}" == "openrouter" ]]; then
  [[ -n "${OPENROUTER_API_KEY:-}" ]] \
    || die "OPENROUTER_API_KEY is not set (env or ~/.hermes/.env)"
else
  die "unknown CALLISTO_HERMES_PROVIDER=${PROVIDER} (want openrouter or nous)"
fi

LOG_DIR="${CALLISTO_OXA_LOG_DIR:-${ROOT}/logs/oxa}"
mkdir -p "$LOG_DIR"
LOG="${LOG_DIR}/${TASK_NAME}.log"
PROMPT="$(cat "$PROMPT_FILE")"
WORKTREE_ABS="$(cd "$WORKTREE" && pwd)"

# Pin the agent to the worktree. --in alone is not enough: OX will happily
# write under $HOME (verified 2026-08-26). Prefix the brief with a hard cwd
# rule and pass --no-restore-cwd. --yolo is required for unattended turns;
# the caller still owns the PTY and can interrupt by PID.
WRAPPED="$(cat <<EOF
WORKING DIRECTORY (mandatory): ${WORKTREE_ABS}
Create, edit, and delete files only under that directory. Do not write to \$HOME, /tmp (except scratch), or any other worktree. Do not touch master. Do not git stash, git reset --hard, or git checkout --.

${PROMPT}
EOF
)"

echo "nous-supervisor: task=${TASK_NAME} worktree=${WORKTREE_ABS} branch=${branch}"
echo "nous-supervisor: model=${MODEL} provider=${PROVIDER} idle_minutes=${IDLE_MINUTES}"
echo "nous-supervisor: log=${LOG}"

hermes \
  --provider "$PROVIDER" \
  -m "$MODEL" \
  --in "$WORKTREE_ABS" \
  --no-restore-cwd \
  --yolo \
  -z "$WRAPPED" \
  2>&1 | tee "$LOG"
exit "${PIPESTATUS[0]}"
