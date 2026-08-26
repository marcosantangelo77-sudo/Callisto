# OX_DONE

Task: split `tools/golf_masters.py` (~1574 lines) into `tools/golf/` package with a facade.

## Done
- `tools/golf/db.py` — DB_PATH, MASTERS_SCHEMA, ensure_masters_schema
- `tools/golf/historical.py` — fetch_masters_historical, ESPN/fallback fetchers, embedded 2010-2025 data, name/position parsing helpers
- `tools/golf/field.py` — fetch_current_season_stats, fetch_masters_field
- `tools/golf/backtest.py` — Spearman correlation, fit-score core, leave_one_out_backtest, rolling_window_backtest
- `tools/golf/predictions.py` — generate_2026_predictions, compute_masters_fit_score
- `tools/golf/__init__.py` — package re-exports
- `tools/golf_masters.py` — now a ~57-line facade re-exporting the full public (and underscore) API; no behavior change for importers (`scripts/masters_preview.py` verified)
- `tests/test_golf_split.py` — 11 tests: facade re-exports, submodule wiring, parsing, embedded data integrity, Spearman, LOO + rolling backtests on seeded temp DBs, fit-score bounds, predictions + composite, field creation/caching

## Verification
- `/tmp/callisto-pytest/bin/python -m pytest tests/test_golf_split.py -q` → 11 passed
- No live betting calls, no full pytest run, master untouched.

## Commit / push
- Commit `refactor(golf): split golf_masters into tools.golf` pushed to `origin/cursor/ox-golf-split-2ac0`.
