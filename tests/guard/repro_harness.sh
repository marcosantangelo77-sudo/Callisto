#!/bin/bash
# Repro harness for the verify-merge.sh blind spot (run: bash repro_harness.sh).
#
# Scenario matrix:
#   1. direct commit deletes a test file
#   2. deletion arrives via a MERGE whose second parent is an autosave branch
#      cut from before the file existed (the real 32ac69e shape)
#   3. rebase/squash-style history rewrite that drops the file
#   4a. merge deletes a public function
#   4b. merge shrinks a source file >25%
# Each scenario asserts OLD guard misses it / NEW guard catches it.
set -u
GUARD_DIR=$(cd "$(dirname "$0")/.." && pwd)
OLD_GUARD="/Users/marcosantangelo/callisto-wt/verify-merge.sh"   # the v1 guard (unmodified)
NEW_GUARD="$GUARD_DIR/verify-merge.sh.new"
PASS=0; TOTAL=0

check() { # check <label> <old-section-caught:yes/no> <new-guard-exit-caught:1>
  TOTAL=$((TOTAL+1))
  local label=$1 old_hit=$2 new_caught=$3
  if [ "$old_hit" = "no" ] && [ "$new_caught" = "catch" ]; then
    echo "  [$label] old guard's OWN check stayed silent, new guard FAILED it — as designed"; PASS=$((PASS+1))
  else
    echo "  [$label] UNEXPECTED: old_section_hit=$old_hit new=$new_caught"
  fi
}
old_section() { # did the old guard print a ✗ in its named section?
  local out=$1 section=$2
  awk -v s="$section" 'index($0,"──")==1{in_s=(index($0,s)>0)} in_s && /✗/{found=1} END{exit !found}' <<<"$out" \
    && echo yes || echo no
}
old_caught_section() { # old guard exit=1 AND its own named section printed ✗
  local out=$1 rc=$2 section=$3
  if [ "$rc" != "0" ] && [ "$(old_section "$out" "$section")" = "yes" ]; then echo yes; else echo no; fi
}
new_caught() {
  bash "$NEW_GUARD" "$1" >/dev/null 2>&1 || echo catch
}

S=$(mktemp -d /tmp/vmguard.XXXXXX)
cd "$S" || exit 9
git init -q . && git config user.email t@t && git config user.name t
mkdir -p tools tests attic

mk_src() { local f=$1; shift; : > "$f"
  for fn in "$@"; do printf 'def %s():\n    return 1\n\n' "$fn" >> "$f"; done; }

mk_src tools/engine.py alpha bravo
printf 'def test_base():\n    assert True\n' > tests/test_base.py
git add -A && git commit -qm "base"

echo "=== scenario 1: DIRECT COMMIT deletes a test file ==="
# (old guard catches this one — kept in the matrix to prove the new guard
#  doesn't regress the case v1 already handled)
git rm -q tests/test_base.py && git commit -qm "oops drop stale test"
out=$(bash "$OLD_GUARD" HEAD~1 2>/dev/null); rc=$?
oh=$(old_caught_section "$out" "$rc" "vanished test files")
nc=$(new_caught HEAD~1)
check "direct-delete" $oh $nc

echo "=== scenario 2: MERGE lands an autosave-branch deletion (real shape) ==="
git reset -q --hard HEAD~1
git checkout -qb feature
printf 'def test_redteam_answer_correctness():\n    assert True\n' > tests/test_redteam_answer_correctness.py
printf '\ndef charlie():\n    return 3\n' >> tools/engine.py
git add -A && git commit -qm "feature: redteam repros + charlie()"
git checkout -q main && git merge -q --no-ff feature -m "merge feature"

# autosave branch cut from BEFORE the test existed; its tree lacks the file,
# so the merge into main carries the deletion as a conflict-free resolution
# of "branch deleted it, base didn't have it" — exactly the 32ac69e shape.
git checkout -qb autosave HEAD~1
git rm -q tests/test_base.py
git checkout -q HEAD~1 -- tools/engine.py 2>/dev/null || true   # keep engine conflict-free; the FILE deletion is the payload
git add -A && git commit -qm "autosave: in-flight work on fix/x"
git checkout -q main
git merge --no-ff --no-edit autosave >/dev/null 2>&1 || { echo unexpected conflict; exit 9; }
# NOTE: v1 hardcodes `cd ~/Documents/GitHub/Callisto` on line 10 — so when run
# from this scratch repo it silently inspects the MAIN Callisto checkout's HEAD
# instead. That is flaw (A) being reproduced live: the old guard cannot be
# aimed at a scratch repo at all, and in production it means every worktree
# invocation graded the wrong tree. We still record what it said.
out=$(bash "$OLD_GUARD" HEAD~1 2>/dev/null); rc=$?
oh=$(old_caught_section "$out" "$rc" "vanished test files")
cd "$S" && nc=$(new_caught "")
check "merge-autosave-delete" $oh $nc

echo "=== scenario 3: REBASE-style drop of the test file ==="
git checkout -q main~0 && git checkout -qb rebased main~2   # pre-test state
git cherry-pick --allow-empty feature >/dev/null 2>&1 || true
out=$(bash "$OLD_GUARD" HEAD~1 2>/dev/null); rc=$?
oh=$(old_caught_section "$out" "$rc" "vanished test files")
nc=$(new_caught HEAD~1)
check "rebase-drop" $oh $nc

echo "=== scenario 4a: MERGE deletes a public function ==="
git checkout -q main
git reset -q --hard main@{1} 2>/dev/null || true
git checkout -qb func main
git checkout -qb side func
printf '\ndef delta():\n    return 4\n' >> tools/engine.py
git add -A && git commit -qm "side: adds delta"
git checkout -q func
printf 'def alpha():\n    return 1\n' > tools/engine.py    # bravo+delta gone
git add -A && git commit -qm "func: rewrite"
git merge -q --no-ff side -m "merge side" >/dev/null 2>&1 || true
out=$(bash "$OLD_GUARD" HEAD~1 2>/dev/null); rc=$?
oh=$(old_caught_section "$out" "$rc" "deleted public functions")
nc=$(new_caught HEAD^1)
check "merge-deletes-func" $oh $nc

echo "=== scenario 4b: source file shrinks >25% across a merge ==="
seq 1 200 | sed 's/^/# filler line /' >> tools/engine.py
git add -A && git commit -qm "grow engine"
o=$(wc -l < tools/engine.py)
awk 'NR<=10' tools/engine.py > tmp && mv tmp tools/engine.py   # shrink ~95%
git add -A && git commit -qm "shrink engine"
out=$(bash "$OLD_GUARD" HEAD~1 2>/dev/null); rc=$?
oh=$(old_caught_section "$out" "$rc" "shrank")
nc=$(new_caught HEAD~1)
check "shrink-detect" $oh $nc

echo
echo "scratch repo: $S"
echo "harness result: $PASS/$TOTAL scenarios behaved as designed"
[ $PASS -eq $TOTAL ]
