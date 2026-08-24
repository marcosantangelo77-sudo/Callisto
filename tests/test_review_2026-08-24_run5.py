"""REVIEW run 12 — 2026-08-24 (reviewer: ox-alpha, standing review role).

Subject under review: the pm1 merge train (32ac69e / caeeeb8 / 2ef77f8,
landed on master 2026-08-24 ~17:51) and specifically whether it preserved
the work merged at db08c13 six hours earlier.

THE DEFECT: commit cd7f068 ("autosave: in-flight work on
fix/worldbank-planner") — an autosave snapshot taken at 10:24, BEFORE the
C5 reconciliation fix and quant-gate tightening existed on that branch's
base — was merged into master as-is. Because autosave commits whatever is
in the working tree, and that tree predated (or had stashed away) the
morning's engine fixes, the merge silently:

  1. DELETED the C5 compute-output↔stance reconciliation fix
     (_sole_bare_boolean binding on leaf direction; 4c09a7b + db08c13).
  2. REVERTED the quant gate to "any digit / any ok sandbox counts"
     (_produced_quantitative -> produced_quant=sandbox-ok-or-digit),
     re-admitting year tokens as quantitative evidence (undid 38aee73).
  3. DELETED four findings files (incl. three review runs' reports and
     arithmetic_contradiction.md) and THREE repro suites:
       tests/test_review_2026-08-24.py      (10 failing repros)
       tests/test_review_0824_audit.py      (17 failing repros)
       tests/test_review_0824_run3.py       (8 failing repros)
       tests/test_redteam_answer_correctness.py (16th-pass suite)
  4. Reverted the speed-golden fixtures regenerated for the C5 fix.

The deletions are not a cleanup: every defect those suites documented is
STILL LIVE on post-merge master. This file restores each deleted suite
into tests/ under its original name and asserts the defects it pins.
Every test here FAILS on origin/master 2ef77f8 for the documented reason.

Family assignment: this is PATTERNS family 4 at process scale — a LABEL
("autosave: in-flight work") deciding what reaches master — compounding
family 2 (a fix lands in one copy while another keeps the bug: master
kept the bug because the train merged an older copy of the whole file)
and family 7 (deleting the red tests makes the suite look greener).

Repro method: `git show db08c13:<suite>` restored verbatim; run against
master. Also direct probes:
  - _sole_bare_boolean absent from master engine.py
  - produced_quant back to sandbox-ok-or-any-digit (engine.py ~line 561)
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


def test_c5_reconciliation_fix_still_exists_on_master():
    """The C5 seam check must exist. It does not — cd7f068 removed it."""
    src = (REPO / "tools" / "pipeline" / "engine.py").read_text()
    assert "_sole_bare_boolean" in src, (
        "compute-output/stance reconciliation (C5, 4c09a7b/db08c13) is "
        "GONE from master — reverted by merging autosave snapshot "
        "cd7f068 (fix/worldbank-planner). A sandbox computation whose "
        "bare-boolean output contradicts the answer no longer forces "
        "refusal; the leaf seals its own arithmetic's negation again.")
    assert "reconciliation_failure" in src


def test_quant_gate_not_reverted_to_digit_matching():
    """38aee73 stopped year tokens and bare sandbox-ok counting as
    quantitative evidence. Master runs the old rule again."""
    src = (REPO / "tools" / "pipeline" / "engine.py").read_text()
    assert "_produced_quantitative" in src, (
        "quant gate reverted: master accepts ANY successful sandbox run "
        "or ANY digit (incl. year tokens like '2023') as quantitative "
        "evidence — the exact behaviour canary-promoted away in 38aee73.")


def test_review_repro_suites_present_on_master():
    """The three review repro suites were deleted by the same merge while
    all 35 of their defects remain live. A repo that deletes its failing
    tests manufactures confidence (PATTERNS family 7)."""
    tests_dir = REPO / "tests"
    missing = [n for n in (
        "test_review_2026-08-24.py",
        "test_review_0824_audit.py",
        "test_review_0824_run3.py",
    ) if not (tests_dir / n).exists()]
    assert not missing, f"deleted review repro suites: {missing}"


def test_findings_files_present_on_master():
    findings = REPO / "findings"
    missing = [n for n in (
        "arithmetic_contradiction.md",
        "review_2026-08-24.md",
        "review_0824_run2.md",
        "review_2026-08-24_run3.md",
    ) if not (findings / n).exists()]
    assert not missing, f"deleted findings records: {missing}"


def test_money_path_fixes_still_absent_from_master():
    """Not caused by cd7f068 (never merged), but re-confirmed live on the
    post-merge master this run reviewed: kelly_full sizes a FULL bankroll
    on decimal odds (D1), kelly_with_push still uses the binary
    denominator (D5), cents quotes inflate EV ~100x (M4/kind). The
    verified remediation branch improve/money-path-landing remains
    unmerged — family 2 at process scale, third instance."""
    from tools.kelly import kelly_full
    try:
        f = kelly_full(0.05, 1.91)  # decimal odds into American API
    except ValueError:
        return  # fixed
    assert f <= 0.25, (
        f"kelly_full(0.05, 1.91) == {f}: decimal odds sized a full "
        "bankroll instead of raising (D1, redteam_money_deep.md)")
