#!/bin/bash
# Post-merge guard v2. Catches the failure classes that a human reviewer — me —
# has repeatedly missed by eye:
#   1. a merge that DELETED a public function another branch added
#   2. test files that vanished (direct commit, MERGE, or rebase)
#   3. a source file that shrank sharply, or was DELETED outright
#   4. imports that no longer resolve
# Run after EVERY merge, before pushing. Exit 1 means do not push.
#
# v2 fixes (see findings/verify_merge_blindspot.md):
#   A. NO hardcoded cd. v1 did `cd ~/Documents/GitHub/Callisto`, so when run
#      inside ANY worktree (merge-train included) it silently inspected the
#      MAIN CHECKOUT's HEAD — a completely different tree. Every check could
#      pass while the merged branch had just lost files. The guard now runs
#      where you invoke it.
#   B. Correct base for merges: v1 defaulted PREV=HEAD~1, which for a merge
#      commit M is M^1's PARENT — skipping the entire first-parent side. v2
#      defaults to the FIRST PARENT of HEAD for merges (the pre-merge state)
#      and HEAD~1 for ordinary commits. Explicit arg still wins.
#   C. Vanished-test check compared against `git show $PREV --name-only`
#      (files touched by ONE COMMIT — empty for most bases) plus an ls-tree
#      whose result it then ignored for fail purposes in the comm() path.
#      v2 compares full trees: every .py under tests/ in PREV must exist now.
#   D. Source files DELETED outright were skipped by `[ -f "$f" ] || continue`.
#      Deletion of a whole non-test module is now a failure, not a skip.
set -u
TOP=$(git rev-parse --show-toplevel 2>/dev/null) || { echo "not a git repo" >&2; exit 2; }
cd "$TOP" || exit 2

# Base selection: for a merge commit default to first parent (pre-merge state);
# otherwise HEAD~1. An explicit argument overrides both.
if [ $# -ge 1 ]; then
  PREV=$1
elif git rev-parse -q --verify HEAD^2 >/dev/null 2>&1; then
  PREV=HEAD^1
else
  PREV=HEAD~1
fi
echo "(guard base: $(git rev-parse --short "$PREV"); HEAD=$(git rev-parse --short HEAD))"
fail=0

echo "── deleted public functions (vs $PREV)"
for f in $(git diff --name-only "$PREV"..HEAD -- '*.py' | grep -vE '^tests/'); do
  [ -f "$f" ] || continue
  old=$(git show "$PREV:$f" 2>/dev/null | grep -oE '^(async )?def [a-z_][a-z0-9_]*' | awk '{print $NF}' | sort -u)
  new=$(grep -oE '^(async )?def [a-z_][a-z0-9_]*' "$f" | awk '{print $NF}' | sort -u)
  gone=$(comm -23 <(echo "$old") <(echo "$new") | grep -v '^_' )
  [ -n "$gone" ] && { echo "  ✗ $f lost: $(echo $gone | tr '\n' ' ')"; fail=1; }
done
[ $fail -eq 0 ] && echo "  ok"

echo "── vanished test files"
missing=""
for t in $(git ls-tree -r "$PREV" --name-only | grep '^tests/.*\.py$'); do
  [ -f "$t" ] || missing="$missing $t"
done
[ -n "$missing" ] && { echo "  ✗ missing:$missing"; fail=1; } || echo "  ok"

echo "── source files that shrank >25% (or were deleted)"
for f in $(git diff --name-only "$PREV"..HEAD -- '*.py' | grep -vE '^tests/'); do
  o=$(git show "$PREV:$f" 2>/dev/null | wc -l | tr -d ' ')
  if [ ! -f "$f" ]; then
    # Whole-module deletion is exactly how a stale branch buries newer work.
    [ "${o:-0}" -gt 100 ] && { echo "  ✗ $f DELETED ($o lines)"; fail=1; }
    continue
  fi
  n=$(wc -l < "$f" | tr -d ' ')
  [ "${o:-0}" -gt 100 ] && [ "$n" -lt $(( o * 75 / 100 )) ] && \
    { echo "  ✗ $f: $o -> $n lines"; fail=1; }
done
[ $fail -eq 0 ] && echo "  ok"

# BASELINE MODE: with --baseline, just print the import failures and exit 0.
# Pre-existing breakage is not merge damage. The supervisor captures a baseline
# BEFORE merging and only fails on failures that are NEW.
echo "── imports resolve"
bad=$(python3 - <<'PY' 2>&1
import importlib, pathlib, sys
sys.path.insert(0, ".")
bad = []
for p in list(pathlib.Path("tools").rglob("*.py")) + list(pathlib.Path("agp").rglob("*.py")):
    if "__pycache__" in str(p) or p.name == "__init__.py": continue
    mod = str(p.with_suffix("")).replace("/", ".")
    try: importlib.import_module(mod)
    except ModuleNotFoundError as e:
        name = (getattr(e, "name", "") or "").split(".")[0]
        if name in {"scipy","mcp","joblib","fastapi","websockets","polars",
                    "xgboost","openpyxl","matplotlib","hypothesis","pandas"}:
            continue
        bad.append(f"{mod}: MISSING FIRST-PARTY {name}")
    except Exception as e: bad.append(f"{mod}: {type(e).__name__}: {str(e)[:70]}")
print("\n".join(bad[:8]))
PY
)
if [ "${BASELINE_MODE:-0}" = "1" ]; then
  echo "$bad"; exit 0
fi
if [ -n "${BASELINE_IMPORTS:-}" ]; then
  newbad=$(comm -13 <(echo "$BASELINE_IMPORTS" | sort) <(echo "$bad" | sort) | grep -v '^$')
  [ -n "$newbad" ] && { echo "$newbad" | sed 's/^/  ✗ NEW: /'; fail=1; } || echo "  ok (pre-existing ignored)"
else
  [ -n "$bad" ] && { echo "$bad" | sed 's/^/  ✗ /'; fail=1; } || echo "  ok"
fi

echo
[ $fail -eq 0 ] && echo "MERGE GUARD: PASS" || echo "MERGE GUARD: FAIL — do not push"
exit $fail
