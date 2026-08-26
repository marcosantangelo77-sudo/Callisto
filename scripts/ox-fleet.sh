#!/usr/bin/env bash
# ox-fleet.sh — keep N Ox Alpha workers fed from a prompt queue.
#
# This is the long loop. Grok should NOT babysit every few minutes.
# Workers push feature branches. Nobody here merges to master.
#
# Usage: bash scripts/ox-fleet.sh
# Env:   CALLISTO_OX_TARGET=6
#        CALLISTO_OX_QUEUE=/tmp/ox_queue/queue.tsv
#        CALLISTO_OX_GIT=/tmp/callisto-merge-master
#        CALLISTO_OX_SUPERVISOR=/workspace/scripts/nous-supervisor.sh
set -euo pipefail

TARGET="${CALLISTO_OX_TARGET:-6}"
QUEUE="${CALLISTO_OX_QUEUE:-/tmp/ox_queue/queue.tsv}"
GIT_ROOT="${CALLISTO_OX_GIT:-/tmp/callisto-merge-master}"
SUPERVISOR="${CALLISTO_OX_SUPERVISOR:-/workspace/scripts/nous-supervisor.sh}"
STATE="${CALLISTO_OX_STATE:-/tmp/ox_fleet}"
CLAIMED="${STATE}/claimed.tsv"
TMUX_CONF="${CALLISTO_OX_TMUX_CONF:-/exec-daemon/tmux.portal.conf}"
LOG="${CALLISTO_OXA_LOG_DIR:-/workspace/logs/oxa}/fleet.log"
IDLE_MINUTES="${CALLISTO_OX_IDLE_MINUTES:-180}"
POLL_S="${CALLISTO_OX_POLL_S:-25}"
PATH="${HOME}/.local/bin:${HOME}/.hermes/bin:${PATH}"
export PATH

mkdir -p "$STATE" "$(dirname "$LOG")" /workspace/logs/oxa

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) $*" | tee -a "$LOG"; }

tmux_cmd() { tmux -f "$TMUX_CONF" "$@"; }

hermes_count() {
  python3 - <<'PY'
import os
n = 0
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        comm = open(f"/proc/{pid}/comm").read().strip()
    except Exception:
        continue
    if comm in ("hermes", "hermes-agent"):
        n += 1
print(n)
PY
}

hermes_in_worktree() {
  local wt="$1"
  python3 - "$wt" <<'PY'
import os, sys
want = os.path.realpath(sys.argv[1])
for pid in os.listdir("/proc"):
    if not pid.isdigit():
        continue
    try:
        comm = open(f"/proc/{pid}/comm").read().strip()
        if comm not in ("hermes", "hermes-agent"):
            continue
        cwd = os.path.realpath(f"/proc/{pid}/cwd")
    except Exception:
        continue
    if cwd == want or cwd.startswith(want + os.sep):
        print(pid)
        break
PY
}

interrupt_zombie() {
  local wt="$1"
  local pid
  pid="$(hermes_in_worktree "$wt" || true)"
  if [[ -n "${pid}" ]]; then
    log "SIGINT hermes pid=${pid} in ${wt} (OX_DONE zombie)"
    kill -INT "$pid" 2>/dev/null || true
  fi
}

# Files currently owned by a live (no OX_DONE) worktree, plus seed locks.
locked_files() {
  python3 - "$GIT_ROOT" <<'PY'
import os, subprocess, sys
git_root = sys.argv[1]
locked = set()
seed = os.environ.get("CALLISTO_OX_SEED_LOCKS", "")
for part in seed.split(";"):
    part = part.strip()
    if part:
        locked.add(part)
for name in os.listdir("/tmp"):
    if not name.startswith("callisto-ox-"):
        continue
    wt = os.path.join("/tmp", name)
    if not os.path.isdir(wt) or not os.path.isdir(os.path.join(wt, ".git")) and not os.path.exists(os.path.join(wt, ".git")):
        # git worktrees have .git file
        if not os.path.exists(os.path.join(wt, ".git")):
            continue
    if os.path.isfile(os.path.join(wt, "OX_DONE.md")):
        continue
    # live if hermes cwd matches OR working tree dirty OR still the original launch
    try:
        out = subprocess.check_output(
            ["git", "-C", wt, "diff", "--name-only", "origin/master"],
            text=True, stderr=subprocess.DEVNULL,
        )
        for line in out.splitlines():
            line = line.strip()
            if line:
                locked.add(line.split("/")[0] + ("/" + line.split("/")[1] if "/" in line else ""))
                locked.add(line)
    except Exception:
        pass
print("\n".join(sorted(locked)))
PY
}

file_conflicts() {
  local files_csv="$1"
  python3 - "$files_csv" <<'PY'
import os, subprocess, sys
need = [p.strip() for p in sys.argv[1].split(",") if p.strip()]
# Any live worktree (no OX_DONE) whose diff or seed overlaps.
live_wts = []
for name in os.listdir("/tmp"):
    if not name.startswith("callisto-ox-"):
        continue
    wt = os.path.join("/tmp", name)
    if not os.path.exists(os.path.join(wt, ".git")):
        continue
    if os.path.isfile(os.path.join(wt, "OX_DONE.md")):
        continue
    live_wts.append(wt)

def owned(wt):
    names = set()
    try:
        out = subprocess.check_output(
            ["git", "-C", wt, "diff", "--name-only", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        )
        names.update(l.strip() for l in out.splitlines() if l.strip())
        out = subprocess.check_output(
            ["git", "-C", wt, "ls-files", "--others", "--exclude-standard"],
            text=True, stderr=subprocess.DEVNULL,
        )
        names.update(l.strip() for l in out.splitlines() if l.strip())
    except Exception:
        pass
    return names

def overlaps(owned_names, need):
    for n in need:
        n = n.rstrip("/")
        for o in owned_names:
            if o == n or o.startswith(n + "/") or n.startswith(o.rstrip("/") + "/"):
                return True
            # top-level file match
            if o.split("/")[0] == n.split("/")[0] and n in ("api.py", "inference.py", "callisto.py"):
                if o == n:
                    return True
    return False

# Seed: known in-flight tasks from env, format wt:file,file;wt:file
seed = os.environ.get("CALLISTO_OX_SEED_LOCKS", "")
for part in seed.split(";"):
    part = part.strip()
    if not part or ":" not in part:
        continue
    wt, files = part.split(":", 1)
    if os.path.isfile(os.path.join(wt, "OX_DONE.md")):
        continue
    # still live if no OX_DONE
    owned_names = {f.strip() for f in files.split(",") if f.strip()}
    if overlaps(owned_names, need):
        print(wt)
        sys.exit(0)

for wt in live_wts:
    if overlaps(owned(wt) | {os.path.basename(wt)}, need):
        # also overlap against seed files for this wt if empty diff (just started)
        print(wt)
        sys.exit(0)
    # empty diff: compare basename task conventions
print("")
PY
}

conflicts_with_live() {
  local files_csv="$1"
  python3 - "$files_csv" "$CLAIMED" <<'PY'
import os, subprocess, sys

need = [p.strip().rstrip("/") for p in sys.argv[1].split(",") if p.strip()]
claimed_path = sys.argv[2]

def matches(path, need):
    path = path.strip().rstrip("/")
    for n in need:
        if path == n or path.startswith(n + "/") or n.startswith(path + "/"):
            return True
        # directory token: tools/signals vs tools/signals/paper.py
        if n.endswith("/") and path.startswith(n):
            return True
    return False

if os.path.isfile(claimed_path):
    with open(claimed_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            _name, files, wt = parts[0], parts[1], parts[2]
            if os.path.isfile(os.path.join(wt, "OX_DONE.md")):
                continue
            for f in files.split(","):
                if matches(f.strip(), need):
                    print("claimed", wt, f)
                    sys.exit(1)

seed = os.environ.get("CALLISTO_OX_SEED_LOCKS", "")
for part in seed.split(";"):
    part = part.strip()
    if not part or ":" not in part:
        continue
    wt, files = part.split(":", 1)
    if os.path.isfile(os.path.join(wt, "OX_DONE.md")):
        continue
    for f in files.split(","):
        if matches(f.strip(), need):
            print("seed", wt, f)
            sys.exit(1)

for name in os.listdir("/tmp"):
    if not name.startswith("callisto-ox-"):
        continue
    wt = os.path.join("/tmp", name)
    if name in ("callisto-ox-dispatch",):
        continue
    if not os.path.exists(os.path.join(wt, ".git")):
        continue
    if os.path.isfile(os.path.join(wt, "OX_DONE.md")):
        continue
    # Only lock files for worktrees that currently have a hermes cwd.
    # Abandoned checkouts (and the dispatch docs tree) must not starve the queue.
    try:
        cwd_real = os.path.realpath(wt)
        has_hermes = False
        for pid in os.listdir("/proc"):
            if not pid.isdigit():
                continue
            try:
                comm = open(f"/proc/{pid}/comm").read().strip()
                if comm not in ("hermes", "hermes-agent"):
                    continue
                if os.path.realpath(f"/proc/{pid}/cwd") == cwd_real:
                    has_hermes = True
                    break
            except Exception:
                continue
        if not has_hermes:
            continue
        out = subprocess.check_output(
            ["git", "-C", wt, "diff", "--name-only", "HEAD"],
            text=True, stderr=subprocess.DEVNULL,
        )
        extra = subprocess.check_output(
            ["git", "-C", wt, "ls-files", "--others", "--exclude-standard"],
            text=True, stderr=subprocess.DEVNULL,
        )
        owned = [l.strip() for l in (out + extra).splitlines() if l.strip()]
    except Exception:
        owned = []
    for o in owned:
        if matches(o, need):
            print("live", wt, o)
            sys.exit(1)
sys.exit(0)
PY
}

ensure_worktree() {
  local branch="$1" dir="$2"
  if [[ -d "$dir" ]]; then
    return 0
  fi
  git -C "$GIT_ROOT" fetch origin master >/dev/null 2>&1 || true
  git -C "$GIT_ROOT" worktree add -b "$branch" "$dir" origin/master
}

launch_task() {
  local name="$1" prompt="$2" files="$3"
  local branch="cursor/ox-${name}-2ac0"
  local wt="/tmp/callisto-ox-${name}"
  local session="ox-${name}"

  if [[ -f "${wt}/OX_DONE.md" ]]; then
    log "skip ${name}: already OX_DONE"
    return 1
  fi
  if conflicts_with_live "$files"; then
    :
  else
    log "skip ${name}: exclusive files busy (${files})"
    return 1
  fi
  if [[ ! -f "$prompt" ]]; then
    log "skip ${name}: missing prompt $prompt"
    return 1
  fi

  ensure_worktree "$branch" "$wt"
  if tmux_cmd has-session -t "=$session" 2>/dev/null; then
    # session exists; if no hermes, start supervisor in it
    if [[ -z "$(hermes_in_worktree "$wt" || true)" ]]; then
      log "relaunch supervisor in existing tmux $session"
      tmux_cmd send-keys -t "$session:0.0" \
        "export PATH=\"${HOME}/.local/bin:${HOME}/.hermes/bin:\$PATH\"; export CALLISTO_HERMES_MAX_PROCS=12; bash ${SUPERVISOR} ${name} ${wt} ${prompt} ${IDLE_MINUTES}" \
        C-m
    else
      log "already live ${name}"
    fi
    printf '%s\t%s\t%s\n' "$name" "$files" "$wt" >> "$CLAIMED"
    return 0
  fi
  tmux_cmd new-session -d -s "$session" -c "$wt" -- bash -l
  tmux_cmd send-keys -t "$session:0.0" \
    "export PATH=\"${HOME}/.local/bin:${HOME}/.hermes/bin:\$PATH\"; export CALLISTO_HERMES_MAX_PROCS=12; bash ${SUPERVISOR} ${name} ${wt} ${prompt} ${IDLE_MINUTES}" \
    C-m
  log "launched ${name} wt=${wt} files=${files}"
  printf '%s\t%s\t%s\n' "$name" "$files" "$wt" >> "$CLAIMED"
  return 0
}

reap() {
  local wt
  for wt in /tmp/callisto-ox-*; do
    [[ -d "$wt" ]] || continue
    if [[ -f "${wt}/OX_DONE.md" ]]; then
      interrupt_zombie "$wt"
    fi
  done
  if [[ -f "$CLAIMED" ]]; then
    python3 - "$CLAIMED" <<'PY'
import os, sys
path = sys.argv[1]
keep = []
if os.path.isfile(path):
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            wt = parts[2]
            if os.path.isfile(os.path.join(wt, "OX_DONE.md")):
                continue
            keep.append(line if line.endswith("\n") else line + "\n")
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(keep)
PY
  fi
}

fill() {
  local running want name prompt files launched
  running="$(hermes_count)"
  want="$TARGET"
  if [[ "$running" -ge "$want" ]]; then
    return 0
  fi
  log "fill: hermes=${running} target=${want}"
  launched=0
  while IFS=$'\t' read -r name prompt files; do
    [[ -z "${name:-}" || "$name" == \#* ]] && continue
    running="$(hermes_count)"
    if [[ "$running" -ge "$want" ]]; then
      break
    fi
    if launch_task "$name" "$prompt" "$files"; then
      launched=$((launched + 1))
      sleep 2
    fi
  done < "$QUEUE"
  log "fill done launched=${launched} hermes=$(hermes_count)"
}

log "ox-fleet start target=${TARGET} queue=${QUEUE}"
if [[ ! -f "$QUEUE" ]]; then
  log "FATAL missing queue $QUEUE"
  exit 2
fi
if [[ ! -x "$SUPERVISOR" && ! -f "$SUPERVISOR" ]]; then
  log "FATAL missing supervisor $SUPERVISOR"
  exit 2
fi

while true; do
  reap
  fill || true
  sleep "$POLL_S"
done
