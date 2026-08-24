# Stance propagation — the parent sealed the WRONG DIRECTION

**Date:** 2026-08-24
**Worktree:** ~/callisto-wt/epistemics, branch fix/stance-propagation
(fast-forwarded from origin/review/deep-audit-0824 @ ca26deb first; commits
4ec05a3, dd4fb18 pushed back to that branch)
**Trigger:** findings/one_real_question_run.json (commit ffeca37):

    Q: "Has the US unemployment rate been lower in 2026 than in January 2023?"
    Verified truth: Jan 2023 = 3.5%; 2026 = 4.3 4.4 4.3 4.3 4.3 4.2 4.1.
    Correct answer: NO.
    System sealed: stance=AFFIRMS, confidence 0.55 PROBABLE.

## DEFECT 1 — stance propagation (FIXED)

### Root cause

`engine.py` (assembly stage) took BOTH the parent's confidence AND the
parent's direction from `best_leaf`, the highest-confidence leaf:

```python
best_leaf = max(answered, key=lambda l: l.confidence)
proposed = best_leaf.confidence
parent_stance = best_leaf.stance      # <-- the defect
```

In this run:

| leaf | sub-question | conf | own stance |
|------|-------------|------|-----------|
| 1 | "what was the Jan 2023 rate?" | 0.95 VERIFIED | AFFIRMS (its own lookup) |
| 2 | "what has 2026 been?" | 0.95 VERIFIED | AFFIRMS (its own lookup) |
| 3 | "how do they compare?" — the ONLY leaf answering the parent claim | 0.54 SPECULATIVE, GAP unprovable | UNDETERMINED |

A factual-lookup leaf affirming ITSELF is not evidence about the parent's
claim. Confidence and DIRECTION are different quantities and were selected
by the same rule.

### The choice

Options weighed:

- (a) only decisional leaves contribute stance;
- (b) no adequate leaf -> parent UNDETERMINED (p=0.5);
- (c) explicit synthesis step deriving parent stance from leaf ANSWERS.

Chosen: **(a)+(b), implemented structurally** — decisional leaves are
identified WITHOUT another model call, so it is deterministic and testable:
a leaf is decisional iff its sub-question text carries a comparative frame
(compare/lower/higher/than/versus/...) AND shares at least one
quantity-bearing token (year or number) with a sibling leaf's text (i.e.
it compares across leaves rather than looking up). Its declared direction
counts only if its own confidence clears TIER_PROBABLE_MIN (0.55) — a leaf
below that has itself said the comparison is unproven. If NO decisional
leaf qualifies, the parent is UNDETERMINED and a note names why.

Option (c) was rejected for now: deriving stance from leaf answers with a
second model call re-introduces exactly the unconstrained-proposal hole of
break B4 (model asserts a direction its evidence does not carry) one level
up, and "no score may be raised" forbids the consistency machinery that
would police it. Structural selection cannot fabricate a direction.

### Guarantees added

- A parent can NEVER inherit direction from a leaf answering a different
  question.
- No confidence path touched: magnitude still comes from best_leaf via
  provenance ceilings; the rule moves stance only, never a number.
- Leaf 3's honest refusal is preserved verbatim: an UNDETERMINED leaf at
  0.54 sets nothing — but neither do its siblings' self-affirmations.

### Test

`tests/test_redteam_stance_propagation.py` runs the FULL pipeline over THE
EXACT live decomposition with fixture-routed FRED evidence:

- `test_lookup_leaves_at_095_cannot_seal_parent_affirms` — 3.5 vs 4.x with
  lookups at 0.95 AFFIRMS must NOT seal AFFIRMS; parent is UNDETERMINED
  with the explanatory note. This failed on the old engine by construction
  (best_leaf.stance == AFFIRMS).
- `test_decisional_leaf_with_declared_direction_sets_stance` — comparison
  leaf declaring DENIES at 0.70 DOES set parent DENIES.
- `test_decisional_leaf_below_confidence_bar_leaves_parent_undetermined`.
- `test_conflicting_decisional_leaves_take_the_higher_confidence_one` — a
  0.90 lookup AFFIRMS cannot outvote a 0.80 decisional AFFIRMS over a 0.70
  decisional DENIES; within the decisional class, higher confidence wins.
- `test_confidence_never_raised_by_the_stance_rule`.

Suite status after the fix: same failures as before the change (28,
pre-existing on ca26deb — env-dependent key tests, money-path red teams,
synthesis-corroboration repros already failing on the base commit, plus
the pre-existing `test_build_i1_integration::test_engine_fetches_from_
multiple_sources`). Zero new failures; all 16 speed goldens + 5 new
red-team tests pass.

## DEFECT 2 — two leaves, same series, different data (FIXED)

### Finding

leaf1 ("Jan 2023 rate?") planned UNRATE with window start=2023-01-01;
leaf3 ("compare numerically") received a body whose visible observations
ended late 2025 — its honest prose said so and it refused to compare.

Root cause chain, per-leaf query params:

1. `_plan_fred` derives start/end ONLY from years named in the LEAF's own
   sub-question (commit 62c802b). A leaf whose text names only "2026"
   gets start=2026-01-01; a leaf naming both years gets a two-year window.
   Same series, different requests — by design, but:
2. FRED observations default to ASCENDING sort with limit=120. For any
   request without an explicit end in the past, the limit cut lands
   mid-series and silently drops the RECENT end. That is how a body can
   "end late 2025" while the live series continues into 2026 — the
   truncation was invisible, so leaf3 read a transport artifact as the
   series' true end.

### Fix

- `tools/sources/query_builder.py::_plan_fred`: pins `sort_order="desc"`
  on every UNRATE-style observations fetch. Any future limit cut retains
  the MOST RECENT observations — the ones trend/comparison questions ask
  about — deterministically, for every leaf.
- `tools/sources/fred.py::series_observations`: when the returned count
  reaches the requested limit, attaches `data["_truncated"] =
  {limit, n_observations, first, last}`. Truncation is now VISIBLE in the
  evidence body the answer model reads and cites; a short body can never
  again read as "the series ends here".

Both changes are plumbing only; existing planner/adapter tests pass
unchanged (46/46).

## DEFECT 3 — source selection / single-source silence (REPORTED, visibility added)

Facts established:

- The run fetched courtlistener and worldbank (INFERRED keyword-junk) for
  a US unemployment question. The natural independent check, BLS, was
  WAF-403 broken that day (health probe: DEGRADED — HTTP 200, zero rows).
- **Independence counting did NOT notice anything wrong**: it counted
  api.stlouisfed.org + api.worldbank.org + www.courtlistener.com as 3
  independent hosts and reported independence=3, which propped up the
  ceiling that let junk-corroborated leaves seal VERIFIED. Host-counting
  is syntactically correct and epistemically hollow here — three hosts
  agreeing on nothing is not corroboration. (Structural fix is out of
  scope: it lives in gate/admission scoring, off-limits.)
- Nothing in the sealed output said it could not check against BLS.

Added (visibility only, no scoring change):

- `IterativeRetriever.retrieve` records `health_notes`: when
  CALLISTO_SOURCE_HEALTH_NET=1 (opt-in, never in tests), candidate sources
  for the leaf are probed and every non-OK verdict lands in the trace as
  e.g. "bls health=DEGRADED: HTTP 200 but ZERO results...".
- Engine assembly copies those notes into result.notes and appends a
  "single-source answer: only N independent host(s)" note whenever an
  answered run rests on <=1 host. A single-source answer can now SAY it
  is single-source.

## Residue (honest)

1. Gate still admits string-overlap junk (courtlistener/WB name lists) —
   pre-existing residual gap #1 in findings/one_real_question.md; fixing
   it touches admission scoring.
2. The decisional-leaf detector is structural (keyword + shared-quantity),
   so a decomposer that phrases a comparison without comparative words
   would fall back to UNDETERMINED — the safe direction. A richer
   question-kind taxonomy (e.g. a "comparative" QuestionKind from the
   Architect) is the clean long-term fix.
3. BLS needs an API key, not code.

## Commits (branch fix/stance-propagation -> origin/review/deep-audit-0824)

- 4ec05a3 defect 2: deterministic FRED windows (sort_order=desc) +
  visible truncation flag.
- b817d03/9eee311 autosave snapshots (engine stance rule + goldens).
- dd4fb18 defect 1+3: decisional-only parent stance; single-source and
  health notes surfaced; red-team test pinned to THE EXACT live case;
  speed goldens regenerated (notes-only diffs — no confidence/tier/seal
  movement).
