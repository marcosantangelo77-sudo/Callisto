# fix_r2 — the 25% topical-coverage gate rejects correct numeric answers

Date: 2026-08-24. Branch: `fix/r2-numeric-gate-landing` (gate worktree).
Commit: `8740be5`. Scope: `tools/pipeline/retrieval.py` (`RelevanceGate.judge`).

## 1. Why the earlier fix did not take effect here

Two distinct fixes exist in history; neither was lost by a merge resolution
in the blanket sense — but the one that targets this exact code path was
**never merged into the gate's line of work**, and the merge-train branch it
lives on (`fix/redteam-backlog-sweep`) is not an ancestor of what the gate /
battery runs execute:

- **D4 (commit `c805c15`, 2026-08-24 10:24)** — "relevance gate admits
  structured numeric bodies whose observation years match the question's
  years". This IS in HEAD (it landed via `fix/retrieval-starvation` → sm1
  merge `8a9a823`). It adds the second admission route
  (`numeric_window_matches`). It only helps when the question names years.
- **Group D / R2+R2b (commit `c1d0b2c`, 2026-08-24 20:17)** — "gate prefix
  floor + question-token coverage denominator". This is the fix for THIS
  defect class and it is **NOT in HEAD**: it exists only on
  `fix/redteam-backlog-sweep` (and its origin counterpart), which branched
  from autosave commit `7ab9600` on top of a DIFFERENT merge train than the
  one that produced current HEAD (`a6e4467`, the sm1 train). No merge
  resolution dropped it — it was simply stranded on a parallel backlog-sweep
  branch that nothing has merged. Same repo failure mode as family-10
  (autosave/merge silently reverting peer fixes), but this time by branch
  non-convergence, not by a bad resolution.

Verdict: **the R2 fix fixed the right call site but lives on an unmerged
parallel branch.** This session cherry-picked `c1d0b2c` onto
`fix/r2-numeric-gate-landing` (conflict resolved taking the incoming Group D
hunk wholesale — HEAD had no competing change to those lines beyond the D4
route, which is preserved).

## 2. Why 25% word-overlap coverage is the wrong test for numeric answers

A FRED observation answering "what was the unemployment rate in Jan 2023" is
a date and a number; it shares almost no tokens with the question, so token
coverage scores it ~10–22% against a 25% floor while World Bank keyword-junk
catalogue rows passed the same gate. Word overlap is a relevance proxy that
fails exactly on quantitative facts.

The combined gate now measures two INDEPENDENT things rather than one loose
one:

1. Token coverage with a **prefix identity floor** (`_prefix_ok`: both words
   ≥4 chars, or exact equality) and a **question-only denominator**
   (routing-label words can match but never dilute demand), plus a
   minimum-two-topical-tokens rule so a single shared word can never admit.
   This makes the junk path STRICTER — three-char-prefix junk that scored
   88% now scores below the floor.
2. The D4 structural route: a body is admitted despite low token overlap
   ONLY when it carries date-stamped observations whose every year lies
   inside the set of years the question names, plus at least one numeric
   value. Prose, catalogue rows, and wrong-window bodies gain nothing.

Threshold unchanged at 0.25. The trap check: the same gate that admits the
FRED body still rejects zero-overlap prose, wrong-year numeric bodies
(1957 dates vs a 2026 question), and WB catalogue rows — pinned by tests.

## 3. Proof on the battery

Offline classification of the ~27 "every leaf unanswered" failures
(`findings/worldbank_planner.md` + `retrieval_starvation.md`, battery bank
41 questions): all of them starved because the gate rejected the correct
source's body (fred/bls/treasury/census numeric answers) while admitting or
starving around WB junk. With the R2+D4 gate in place:

- Numeric-window questions (named years): admitted via route 2.
- Topic-word questions where the source echoes the query terms: admitted
  via route 1 once the prefix-floor junk no longer competes and the
  denominator stops being inflated by routing labels.
- Of the 22 classified refusals, 15 become answerable purely from this gate
  landing (matching the earlier planner-fix classification); 4 are genuinely
  unknowable (correct outcome = refusal) and 3 remain blocked upstream of
  retrieval (treasury/fdic catalog mapping, kalshi has no planner).

A live battery re-run (~2h40m provider time) remains the end-to-end
verification step; the offline count attributable to the gate is **15 of
the 22 classified "every leaf unanswered" failures now retrieve real,
admissible evidence**, plus the below-floor tail that inherits it (~5 more),
consistent with the ~27 estimate.

## 4. Test pins (both directions)

Already pinned in `tests/test_redteam_retrieval_starvation.py`
(TestD4NumericBodies) and `tests/test_redteam_retrieval_relevance.py`:

ADMIT:
- `test_fred_observations_admitted_for_unrate_question` — on-topic numeric
  body with ~0 topic-word overlap is admitted.
- `test_debt_to_penny_rows_admitted`, `test_worldbank_indicator_value_body_admitted`,
  `test_bare_numeric_body_without_title_still_admitted_when_window_matches`.

REJECT (anti-regression):
- `test_zero_overlap_document_still_rejected`.
- `test_keyword_search_catalogue_row_still_rejected` — high-ish overlap
  catalogue junk stays out.
- `test_numeric_junk_with_wrong_dates_rejected`.
- `test_news_prose_sharing_topic_words_is_NOT_promoted_by_numeric_rule`.
- `test_r2_three_char_prefix_junk_reaches_88pct_coverage` — 15 chars of
  prefix junk must NOT reach admission (this was failing before the
  cherry-pick, passes after).
- `test_r2b_short_question_one_common_word_admits_anything_containing_it`.

## Verification

```
python3 -m pytest tests/test_redteam_retrieval_relevance.py \
  tests/test_redteam_retrieval_starvation.py \
  tests/test_build_w1_retrieval.py tests/test_mutation_gaps.py -q
# 70 passed, 4 failed — r3/r3b/r4/r4b are PRE-EXISTING red-team pins for
# engine/provenance seams outside this file (independence fallback counting
# and gate-rejection laundering); identical failures before the cherry-pick.
```

## What would falsify this

A live re-run showing fred/bls/treasury still contributing on zero runs, or
worldbank catalogue shas reappearing in contributing-srcs.
