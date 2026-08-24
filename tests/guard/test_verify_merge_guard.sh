#!/bin/bash
# Self-test for the merge guard (verify-merge.sh.new).
# Runs the guard's four checks against purpose-built scratch histories and
# asserts each check fires when it must — including on merge commits, where
# the correct base is HEAD^1 (first parent), not HEAD~1.
#
# Usage: bash test_verify_merge_guard.sh   (exit 0 = all assertions hold)
set -u
GUARD="$(cd "$(dirname "$0")/.." && pwd)/verify-merge.sh"
[ -f "$GUARD" ] || GUARD="/Users/marcosantangelo/callisto-wt/guard-tests/verify-merge.sh.new"
[ -f "$GUARD" ] || { echo "guard script not found next to this test"; exit 2; }
PASS=0; FAIL=0

assert_fails_with() { # assert_fails_with <label> <expected-substring>
  local label=$1 want=$2 out rc
  out=$(bash "$GUARD" 2>/dev/null); rc=$?
  if [ $rc -ne 0 ] && printf '%s' "$out" | grep -qF "$want"; then
    PASS=$((PASS+1)); echo "ok   - $label"
  else
    FAIL=$((FAIL+1)); echo "FAIL - $label (exit=$rc, wanted '$want' in output)"
    printf '%s\n' "$out" | sed 's/^/       /'
  fi
}
assert_passes() { # assert_passes <label>
  local label=$1 rc
  bash "$GUARD" >/dev/null 2>&1 && rc=0 || rc=$?
  # import-check noise from unrelated env breakage is tolerated only via
  # BASELINE_IMPORTS, which we set below to whatever the clean base has.
  if [ $rc -eq 0 ]; then PASS=$((PASS+1)); echo "ok   - $label"
  else FAIL=$((FAIL+1)); echo "FAIL - $label (exit=$rc)"; fi
}

T=$(mktemp -d /tmp/vmguard-selftest.XXXXXX)
trap 'rm -rf "$T"' EXIT
cd "$T" || exit 9
git init -q . && git config user.email t@t && git config user.name t
mkdir -p tools tests

# Clean baseline: one module (alpha+bravo), one test. Silence the import
# section by pointing BASELINE_IMPORTS at whatever a clean run reports.
printf 'def alpha():\n    return 1\n\ndef bravo():\n    return 2\n' > tools/engine.py
printf 'def test_ok():\n    assert True\n' > tests/test_base.py
git add -A && git commit -qm "base"
CLEAN_BASE=$(BASELINE_MODE=1 bash "$GUARD" 2>/dev/null)
export BASELINE_IMPORTS="$CLEAN_BASE"

echo "== 1. vanished test file: direct commit"
git rm -q tests/test_base.py && git commit -qm "drop test"
assert_fails_with "direct-commit deletion caught" "vanished test files"

echo "== 2. vanished test file: arrives via MERGE (second parent lacks it)"
git reset -q --hard HEAD~1
git checkout -qb feat
printf 'def test_redteam():\n    assert True\n' > tests/test_redteam.py
git add -A && git commit -qm "feat adds redteam test"
MAIN=$(git symbolic-ref --short HEAD); git checkout -q "$MAIN" && git merge -q --no-ff feat -m "merge feat"
# autosave branch cut BEFORE the test existed deletes an older test file:
git checkout -qb auto HEAD~1
git rm -q tests/test_base.py
git commit -qm "autosave: in-flight work"
git checkout -q "$MAIN"
git merge -q --no-ff auto -m "merge auto" 2>/dev/null || true
assert_fails_with "merge-carried deletion caught" "tests/test_base.py"

echo "== 3. deleted public function across a merge"
git MAIN=$(git symbolic-ref --short HEAD) 2>/dev/null || MAIN=main
git checkout -qb funcfeat
printf '\ndef charlie():\n    return 3\n' >> tools/engine.py
git add -A && git commit -qm "add charlie"
git checkout -q "$MAIN"
printf 'def alpha():\n    return 9\n' > tools/engine.py   # bravo+charlie vanish
git add -A && git commit -qm "rewrite engine"
git merge --no-ff --no-commit funcfeat >/dev/null 2>&1 || true
printf 'def alpha():\n    return 9\n' > tools/engine.py   # resolve: keep the rewrite
git add -A
git -c core.editor=true merge --continue 2>/dev/null || git commit -qm "merge funcfeat (carries bravo/charlie deletion)"
out=$(bash "$GUARD" 2>/dev/null); rc=$?
if [ $rc -ne 0 ] && printf '%s' "$out" | grep -q "MERGE GUARD: FAIL"; then
  PASS=$((PASS+1)); echo "ok   - merge carrying deletions is failed by the guard"
else
  FAIL=$((FAIL+1)); echo "FAIL - public-function deletion caught"; printf '%s\n' "$out" | sed 's/^/       /'
fi

echo "== 4. source file shrinks >25%"
seq 1 200 | sed 's/^/# pad /' >> tools/engine.py
git add -A && git commit -qm "pad engine to 200+ lines"
awk 'NR<=12' tools/engine.py > .tmp && mv .tmp tools/engine.py
git add -A && git commit -qm "shrink engine"
assert_fails_with ">25% shrink caught" "lines"

echo "== 5. whole-module deletion caught"
PAD=$(git log --oneline | awk '/pad engine/{print $1; exit}')
git checkout -q "$PAD"
git rm -q tools/engine.py || git rm -qf tools/engine.py
git commit -qm "autosave: in-flight work (module gone)"
assert_fails_with "whole-module deletion caught" "DELETED"
assert_fails_with "whole-module deletion caught" "DELETED"

echo "== 6. healthy history passes"
git checkout -q -b healthy "$PAD"
assert_passes "clean tree passes all four checks"

echo
if [ $FAIL -eq 0 ]; then echo "SELF-TEST: $PASS passed, 0 failed"; exit 0
else echo "SELF-TEST: $PASS passed, $FAIL FAILED"; exit 1; fi
