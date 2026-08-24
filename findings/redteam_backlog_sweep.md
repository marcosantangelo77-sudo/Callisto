# Red-team backlog sweep — 28 failures grouped by root cause

Date: 2026-08-24. Worktree: `gate`, branch `fix/redteam-backlog-sweep`.
Baseline: 51 failures on master; the four files below carry 28 of them
(13 pass, 8 strict-xfail canaries).

## Method

Each failure was reproduced on the branch, then traced to its production
code path and to git history (`git log -S`, cross-branch diffing) before
anything was fixed. PATTERNS.md's nine families were applied to every one.

## Headline finding

**The 28 failures are NOT 28 defects. They group into ~11 root causes,
and the single largest group (9 of 28) is one event, not nine bugs: an
autosave snapshot (`4a59aa1`, branch fix/retrieval-starvation) silently
REVERTED already-reviewed fixes in tools/pipeline/engine.py when its
branch merged through sm1.** A second group (10 of 28) has complete,
reviewed fixes sitting UNMERGED on other branches
(`improve/money-path-landing`, `fb039fb`). This is PATTERNS family 2
("a fix lands in one copy while another keeps the bug") elevated to the
branch level — review run 12 flagged the identical mechanism
(findings d1a1396, f484ab1) and it happened again the same day.

## Grouping table

| Grp | Tests | Root cause | Family | Status of a fix |
|-----|-------|------------|--------|-----------------|
| A | ac×9: digit_in_prose (ImportError), single_source_seal, summary_distinguishes, answer_may_not_contradict, ComputeReconciliation ×5 | Autosave snapshot `4a59aa1` deleted the C5 compute↔stance reconciliation block, `_produced_quantitative`/`_prose_carries_quantity`, `_sole_bare_boolean`, C4 asked-vs-answered notes, and the `n_sources_answered` summary field from engine.py | 2 (fix lost in another copy) + 10 (merge-time silent revert) | Fix EXISTS — restore from `db08c13`/`4a59aa1^`; merge dropped it |
| B | mp×7: m1, m1b, m2, m2b, m3, m4, m5 | Crossed-book devig (M1/M1b), round() raising Kelly (M2/M2b), Kelly priced at raw payout while edge uses devigged (M3), auto-kind cents misread (M4), summary rounding up (M5). All five defects FIXED on `improve/money-path-landing` (commits 60bb1cf, f6a8615, 162ea18); branch never merged | 2 (third instance of money-path fix stranded) | Fix EXISTS — port |
| D | rr×6: r2, r2b, r3, r3b (+r4, r4b see below) | Relevance-gate prefix hole (3-char junk = 88% coverage; one-word admission) and engine no-trace fallback counting raw source names (+sandbox) as independent voices. Fixed in `fb039fb` on another lineage; never reached master | 2 + 5 | Fix EXISTS for r2/r2b/r3/r3b — port |
| C1 | sc: s1, s1b | Contradiction detection blind spots: values stated inside claim text are never extracted (extract_values runs only on bodies); max(abs)-per-item value selection hides a share contradiction behind agreeing revenue figures (and manufactures fake ones) | 9 + 6 (direction of error) | Real defect, unfixed |
| C2 | sc: s1c | Stance defaults to ""; unspecified stance reads as support, so a refutation facing silence raises no contradiction | 3 (absence treated as success) | Real defect, unfixed |
| C3 | sc: s2b | Ten mirrors of one document under ten distinct names = ten independence units; indep_key is name/host-derived and mirrors control it | 5 (structural property ≠ agreement) | Real defect, unfixed |
| C4 | sc: s3 | Report confidence = MAX over groups; one lucky PRIMARY group outweighs INFERRED filler everywhere else; parent inherits via best-leaf rule | 9 | Real defect (design), unfixed |
| C5 | sc: s4 | classify_null reads "reachable + rejected with reasons" as honest literature null without naming the sources' standing (hostile mirror ⇒ authoritative-sounding absence) | 4 (label standing in for evidence) | Real defect, unfixed |
| D2 | rr: r4, r4b | TEST asserts a ledger-level invariant the ledger cannot know: `assign_source_class` correctly returns PRIMARY for recorded-primary bytes because the test never binds a gate rejection (`record_gate_rejection`). Through the real pipeline the rejection IS bound (retrieval.py:780) and the bytes are superseded | 7 (test passing/failing for the wrong reason) | Argue in findings; candidate LEAVE-RED |

## Count check

A=9, B=7, D=4+2, C=6 ⇒ 28. Distinct root causes ≈ 11 (A; M1; M2; M3;
M4; M5; gate-prefix; indep-fallback; C1 numeric-blindness; C2 stance
default; C3 mirrors; C4 max-aggregation; C5 null-standing; D2
test-level). Several pairs share one mechanism (m1+m1b; m2+m2b;
s1+s1b; r2+r2b; r3+r3b).

## Order of work

1. **Group A** (largest, mechanical restoration, verified by history).
2. **Group D** (port fb039fb gate floor + fallback rule).
3. **Group B** (money path — READ-ONLY discipline preserved; the fixes
   are arithmetic-only, nothing armed, order-scanning tests untouched).
4. **Groups C1–C5** (design-level; each judged on merits).
5. **D2**: argued, expected to stay red per the hard rule.

No confidence score may be raised anywhere; all ports preserve the
only-lower/refuse direction of the original fixes.
