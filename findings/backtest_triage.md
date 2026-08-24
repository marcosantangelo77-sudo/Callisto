# Triage: tests/test_backtest_e2e.py — 11 long-standing failures

Date: 2026-08-23. Worktree: gate (branch build/source-health at start; triage
landed from there). Baseline at session start: **34 failures** suite-wide, of
which these 11 were the oldest unexplained red.

## TL;DR

The 11 failures were **already correctly triaged and fixed once**, on
2026-04-23, in commit `637729a` on branch `origin/fix/broken-tests` — but that
commit was never merged to `main`. The 318 commits on main since 2026-04-23
diverged from it, and the failures silently became "known". Every root cause
below was confirmed by direct reproduction in this worktree before the fix was
applied; nothing was taken on trust from the old commit message.

Verdict per failure: **(b) stale test fixtures/assertions vs deliberately
hardened production gates — 10 failures; (c) environment gap (missing scipy) —
1 failure pair.** No category-(a) production defects. No assertions weakened;
no bare skips added.

## Root causes (verified by reproduction)

1. **`MIN_BOOKS_FOR_SIGNAL = 4` signal gate** (`tools/backtest.py`, introduced
   2026-03-27 in `31ec22f`/`64ef67c` after empirical evidence that signals with
   <4 non-target books were noise): the fixture snapshot had only 3 books total
   = 2 non-target. Every event computed a real edge but `is_signal` was False,
   so `signal_generated=0` everywhere → "0 out of 6 events" assertion failure.
   This is family #7's inverse: the test failed for the right *code path*
   (signal gate) but for an unmodelled reason (fixture too thin), not because
   resolution or devigging broke.
2. **FWER `side_filter_required` hard gate** (2026-04-22, `ef1e495`):
   binary-both-sides markets (totals, h2h) without `side_filter` are refused at
   run time to prevent double-counted events. The totals/h2h tests built
   hypotheses without one → engine returned `{"error": "side_filter_required"}`
   → `assert 'error' not in result` failed.
3. **Per-event signal collapse**: `_get_backtest_signals` collapses book-level
   rows to one signal per unique `event_id`; a single seeded game gives
   sample_size=1, so evaluate/recalculate tests got 0 wins + 0 losses.
4. **Signed spread lines** (`8f6b4c6` fixed the unsigned-line bug that inflated
   ATS hit rates): `line` is stored signed (home=-3.5), so the old push-test
   expectation (home -5.0 winning by 5 → "won") resolved as push. The *test*
   encoded the pre-fix behaviour; production had already been corrected.
5. **`closing_lines` table absent from the test fixture**: created at runtime by
   `CLVTracker.initialize()` in production, but queried directly by
   `data_collector.resolve_game_level_outcomes` → `sqlite3.OperationalError:
   no such table: closing_lines` in all three paper-trade tests.
6. **scipy missing from this environment**: `_recalculate_run_stats` imports
   `scipy.stats.binomtest/ttest_1samp` for p-values/Brier/IC when a run has
   decided events. scipy was not installed locally (not in requirements.txt
   either — see below). This masked causes 1–5 in the two RecalculateRunStats
   tests only after everything else was fixed.

## Triage table

| # | Test | Class | Verdict | Root cause | Action |
|---|------|-------|---------|-----------|--------|
| 1 | test_full_pipeline | TestBacktestEndToEnd | (b) | MIN_BOOKS_FOR_SIGNAL=4 vs 3-book fixture; single-game sample collapse | Fixture expanded to 5 books (DK target + FD/MGM/Caesars/PointsBet); second game seeded |
| 2 | test_totals_resolution | TestBacktestEndToEnd | (b) | FWER side_filter_required gate rejects both-sides totals hypothesis | Hypotheses marked `legacy=True` (documented grandfather path in backtest.py:279) |
| 3 | test_h2h_resolution | TestBacktestEndToEnd | (b) | same gate, h2h market | same |
| 4 | test_spreads_push | TestBacktestEndToEnd | (b) | line stored signed since 8f6b4c6; test asserted unsigned-line semantics | expectations updated to signed-line push semantics |
| 5 | test_away_team_wins_h2h | TestBacktestEndToEnd | (b) | FWER side_filter gate + h2h heavy-fav cutoff (>80% fair prob) | fixture h2h prices kept under 80% fair; legacy=True |
| 6 | test_backtest_run_record | TestBacktestEndToEnd | (b) | signal collapse → sample_size=1 | second seeded game |
| 7 | test_resolve_spread_paper_trade | TestPaperTradeResolution | (c)→fixed | closing_lines table missing from test schema (created by CLVTracker at runtime in prod) | table added to db_path fixture (matches CLVTracker DDL) |
| 8 | test_resolve_totals_paper_trade | TestPaperTradeResolution | (c)→fixed | same missing table | same |
| 9 | test_resolve_h2h_paper_trade | TestPaperTradeResolution | (c)→fixed | same missing table | same |
| 10 | test_deferred_resolution_updates_run | TestRecalculateRunStats | (b)+(c) | causes 1–5 above, then scipy import error | all applied + scipy installed |
| 11 | test_global_resolution_recalculates_all_stale_runs | TestRecalculateRunStats | (b)+(c) | same | same |

## Why the tests are now honest (family #7 check)

Per PATTERNS.md #7's inverse ("a test can also FAIL for the wrong reason"), I
confirmed each failure mode by breaking/observing the actual boundary:

- Reproduced cause 1 directly: with the 3-book fixture the engine emits real
  edges (e.g. DK home edge +1.6%) yet `signal_generated=0` on every row —
  the gate, not the devig math, was the decider.
- Cause 2 is a deliberate production gate with its own unit suite
  (`tests/test_side_filter_required.py`); the e2e tests exercise BOTH sides by
  design, so the documented `legacy=True` grandfather flag is the correct,
  argued accommodation — it does not disable any check for real hypotheses.
- The `closing_lines` fixture DDL mirrors `tools/clv_tracker.py` exactly.
- After the fix, mutating `MIN_BOOKS_FOR_SIGNAL` logic or removing the
  side_filter gate makes specific assertions fail again (the tests still reach
  their subjects).

## Environment note (action item, NOT silently papered over)

`scipy` is imported lazily inside `_recalculate_run_stats`
(tools/backtest.py:3579) but is **absent from requirements.txt**. Any fresh
environment hits `ModuleNotFoundError` mid-recalculation — in production this
would leave run stats silently stale (the pre-fix state those stats gates
depend on). Installed scipy 1.17.1 locally; adding it to requirements.txt is
recommended follow-up for whoever owns that file.

## Result

tests/test_backtest_e2e.py: **40 passed, 0 failed** (was 11F/29P).
tests/test_prop_scanner.py::test_finds_edge (same stale-fixture family,
12th red): passes. Suite baseline drops from 34 to 21 failures; remaining
reds are outside this file's scope.
