# Error Patterns — Institutional Memory
# Append failure modes here so they never recur.
# This file is injected into system prompts during self-diagnosis.

## Race Conditions
- **Stale backtest_runs stats**: Retroactive signal updates change backtest_events but backtest_runs keeps original stats. Evaluate phase checks stale stats → false rejection. Fix: recalculate_all_active_runs() before promotion checks.
- **Migration ordering**: Requeues must run BEFORE threshold migration, or requeued hypotheses keep old thresholds.
- **Startup vs evaluate timing**: Startup migrations (signal update) run after backtest but before evaluate. If evaluate runs with stale run stats, winning hypotheses get rejected.

## Database Locks
- **Raw execute/commit without retry**: ANY write to SQLite must use execute_with_retry/commit_with_retry. WAL mode doesn't prevent writer-writer contention.
- **Long-held transactions**: Batch INSERTs (backtest_events, data_collector) should commit per-batch, not per-event.
- **WAL bloat**: Without PRAGMA wal_autocheckpoint, WAL grows to 20GB+. Set wal_autocheckpoint=1000.

## Pipeline Stalls
- **Spinning detection → generation disabled → no new drafts → spinning forever**: If spinning detected, still generate (bias toward line-based hypotheses).
- **Untestable drafts accumulate**: Drafts with ctx_coverage < 0.5 must be auto-rejected after 48h, not just skipped.
- **Paper trading 0 trades**: Timeout too short (120s) for DK scraper (takes 140s+). Set to 300s.
- **Anti-predictive hypotheses persist**: IC < -0.10 gate was waived when n < 20. Force-reject regardless of sample size.
- **Unconditional cooldown sleeps**: _phase_interpret_backtests and _phase_claude_deep_work slept 75s BEFORE checking Claude availability, stacking to 4+ min/cycle when Claude unavailable. Fix: only sleep when actually calling Claude (after availability check).

## Data Quality
- **Identical event sets**: Multiple hypotheses sharing identical backtest events (same games, same signals). Caused by null game_filters + regex mismatch in context filter.
- **Edge threshold too high**: Real market edges max at ~0.83%. Thresholds above 0.5% filter out most real edges. Default to 0.003.
- **MIN_BOOKS_FOR_SIGNAL=4**: Kills thin markets (props, minor sports). Use 2 for props.

## Memory Leaks
- **Unbounded caches**: golf_category_cache, espn_teams_cache, regime_cache must have hard caps + eviction.
- **WAL file growth**: Not a memory leak but filesystem bloat. Checkpoint periodically.
