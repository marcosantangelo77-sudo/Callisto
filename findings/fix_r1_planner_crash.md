# R1 fix — planner hard crash on Wikidata hint sort (2026-08-24)

## Defect
`tools/sources/query_builder.py:820` (`_plan_wikidata_concept`) sorted the
matched `(Q-id, hint-word)` tuples with `key=lambda p: -p[1]` — unary minus
on a STRING. Any question containing a Wikidata hint word (company,
country, person, drug, …) raised `TypeError` and killed the entire question
run. Regression from the planner work merged 2026-08-24; battery re-run
trigger was `gdelt_02` ("Do any GDELT-indexed news articles mention the
exact phrase 'callisto battery test'?") — went from refused to hard crash.

## Root cause and intent
The tuple layout is `(Q-id, hint)`, both strings. The sibling resolver at
line ~371 sorts `(len(concept), candidates)` with `-p[0]` and comments
"longer = more specific". The intended ordering here is the same: prefer
the longest matching hint ('companies' over 'company'). Fix:
`matched.sort(key=lambda p: -len(p[1]))`.

## Grep for the same shape
Searched `-x[N]` unary-minus-on-tuple-element across tools/:
- `query_builder.py:371` — `-p[0]` where `p[0] = len(concept)` → numeric, correct.
- `gaps.py:243`, `injury_model.py:1034` — numeric elements, correct.
- `sharp_detection.py:219` — not a sort key (direction pair), fine.
No other string-negation instances found.

## Changes
1. `tools/sources/query_builder.py` — sort key is now `-len(p[1])`.
2. `tests/test_redteam_r1_planner_crash.py` — reproduces via hint match,
   pins longer-hint-wins semantics, and runs the exact gdelt_02 battery
   question through `build_plan`.
3. Planner exceptions should not kill a question: the planner-mode routing
   loop in `tools/pipeline/retrieval.py` (~line 589) now wraps
   `build_plan` in try/except; an exception degrades THAT source to an
   honest gap in `trace.skipped_sources` ("planner error: …") while other
   sources continue. (The per-source fetch path already had this
   degradation; only the routing loop lacked it.)
4. `tests/test_r1_planner_degradation.py` — end-to-end through
   `retrieve()` in planner mode with `build_plan` forced to raise;
   asserts the run completes and records the planner-error gap.

## Verification
- Pre-fix reproduction confirmed (TypeError traceback at line 820).
- All 4 targeted test files pass (44 passed).
- Full retrieval/w5/redteam sweep: every failure identical to a pristine
  HEAD baseline worktree (/tmp/r1-baseline) — pre-existing, unrelated.

No confidence score raised. ~/Documents/GitHub/Callisto untouched.
