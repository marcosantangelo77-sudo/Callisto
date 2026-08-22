# COVERAGE MAP — Wave 1 cartography, real per-module measurement

**Status: VERIFIED.** Every number below was measured, not inferred: full test
suite executed on this machine (2026-08-21) with `coverage.py` line coverage,
merged with the 12 known-failing tests' bodies via `--cov-append`. Raw evidence:
`coverage.json` (committed alongside this file).

## Methodology

```
.venv/bin/python -m pytest -q --timeout=120 \
  --ignore=tests/test_ml_classifier.py --ignore=tests/test_ml_drift.py \
  --deselect <12 known failures, see below> \
  --cov=tools --cov=agp --cov=. \
  --cov-report=json:coverage.json --cov-report=term-missing:skip-covered \
  -p no:cacheprovider
# then, so failing test BODIES still count (they fail at assertion, after
# exercising backtest/prop_scanner code):
.venv/bin/python -m pytest -q --timeout=90 <the same 12 tests> \
  --cov=tools --cov=agp --cov=. --cov-append --cov-report= -p no:cacheprovider
.venv/bin/python -m coverage json -o coverage.json   # merged dataset
```

The 12 deselected-then-appended tests: 11 × `tests/test_backtest_e2e.py`
(TestBacktestEndToEnd::{test_full_pipeline,test_totals_resolution,test_h2h_resolution,
test_spreads_push,test_away_team_wins_h2h,test_backtest_run_record},
TestPaperTradeResolution::{test_resolve_spread,test_resolve_totals,test_resolve_h2h}_paper_trade,
TestRecalculateRunStats::{test_deferred_resolution_updates_run,
test_global_resolution_recalculates_all_stale_runs}) + 1 ×
`tests/test_prop_scanner.py::TestPropScanner::test_finds_edge`.

Run result: **1006 passed, 8 skipped, 12 failed (known), ~116 s wall.**
Totals (incl. tests/ and scripts/ themselves): 48,610 statements, 21,996 covered,
**45.2%**. xgboost/libomp modules (`tools/ml_classifier.py`,
`tools/ml_drift.py`) excluded from execution → their 0% is an artifact of THIS
machine, not necessarily of the suite; `ml_backtest.py` (0%) is NOT
xgboost-gated and its 0% is real.

"Lines" = raw `wc -l`; "stmts" = coverage.py statements; "covered" = executed
statements. A module counts as having a named test iff `tests/test_<basename>.py`
exists.

## THE CORRECTED CLAIM

Mandate §1 said:

> 33 modules over 500 lines (~42%) have no test file named after them.

Measured today:

| claim element | mandate | measured now | verdict |
|---|---|---|---|
| modules >500 raw lines | 33 | **63** | count drifted — codebase grew since the census |
| of those, no named test file | 33 | **43** | proxy UNDERCOUNTS: more unnamed modules than claimed |
| combined size of unnamed set | 57,129 ln (~42%) | **63,607 ln** | larger |
| "the money path is untested" | asserted | **FALSE in part** | corrected below |

The proxy ("no `test_X.py`") is directionally useful but wrong in both
directions, which is why it had to be subsumed by real measurement:

- **7 of the 43 unnamed >500-line modules have ≥50% REAL indirect coverage**
  (`schema.py` 57.5%, `arbitrage_scanner.py` 68.1%, `followup_guard.py` 71.4%,
  `inference.py` 53.8%, `live_state.py` 56.9%, `order_manager.py` 86.7%,
  `live_edges.py` 85.9%). The named-test proxy called these untested.
- Conversely a *named* test file guarantees nothing: `tools/ml_classifier.py`
  has `tests/test_ml_classifier.py` and measures **0% here** (env-gated);
  `tests/test_prop_scraper_free.py` exists yet `prop_scraper_free.py`
  measures 12.3%.

### The money-path correction (TIER 0)

Mandate: *"The entire path from decision to real money — sizing, execution, and
the metric that guards promotion — is untested."* Measured against reality:

- `bet_executor.py` — 46.3% stmts covered, earned by **mock-free behaviour
  tests**: `tests/test_portfolio_sizing.py` (per-game/per-sport exposure caps,
  pinned env caps, numeric asserts), `tests/test_drawdown_kill.py`,
  `tests/test_bankroll_race.py`, `tests/test_regime_sizing.py`. This is genuine
  characterization of the sizing caps — the most important TIER 0 arithmetic.
- `kelly.py` — 39.6%, reached through those same executor tests (quarter-Kelly
  with dampener at signals_n=150). Half-covered, and the covered half is the
  sizing core; uncovered half includes edge branches worth auditing.
- `clv_tracker.py` — 45.7% via `tests/test_clv_units.py` (sign convention,
  magnitude bounds, DB column write) and `tests/test_clv_paper_trades.py`.
- `bankroll_sim.py` — 77.5%, named test exists.
- Order lifecycle: `order_manager.py` 86.7% / `order_reconciler.py` 83.3%
  (named tests).

So: **sizing and CLV are partially characterized; execution plumbing beyond
sizing (broker submission, partial fills, error paths in `bet_executor`) is
not; nothing here arms live execution** (mandate rule §5.2 respected — these
are existing tests reading dormant code, plus one measurement pass).

What remains genuinely untested mass, sorted by risk score
(`raw_lines × tier_weight × uncovered_fraction`; weights stated because they are
mine: T0 money-path 1.0, T1 unattended-loop 0.8, T2 gate 0.7, T3 epistemics 0.6,
T4 data-plane 0.4, T5 serving 0.4, other 0.15):

### Top of the true untested mass (unnamed-test modules, worst first)

| module | lines | tier | cov | risk score |
|---|---|---|---|---|
| `tools/autonomous.py` | 7955 | T1 unattended-loop | 3.6% | 6134 |
| `tools/backtest.py` | 4211 | T2 gate | 34.4% | 1935 |
| `api.py` | 4685 | T5 serving | 23.2% | 1439 |
| `orchestrator.py` | 1896 | T1 unattended-loop | 16.7% | 1263 |
| `tools/data_collector.py` | 3156 | T4 data-plane | 10.8% | 1126 |
| `tools/edge_scanner.py` | 1507 | T0 money-path | 42.0% | 873 |
| `tools/pipeline_integrity.py` | 1191 | T1 unattended-loop | 12.3% | 836 |
| `tools/hypothesis_generator.py` | 1684 | T2 gate | 38.8% | 722 |
| `tools/line_monitor.py` | 1958 | T4 data-plane | 8.0% | 721 |
| `tools/self_repair.py` | 1031 | T1 unattended-loop | 17.1% | 684 |
| `tools/bet_executor.py` | 1253 | T0 money-path | 46.3% | 673 |
| `tools/schema.py` | 1981 | T2 gate | 57.5% | 589 |
| `tools/pace_model.py` | 1405 | T4 data-plane | 0.0% | 562 |
| `tools/line_analysis.py` | 1485 | T4 data-plane | 5.6% | 560 |
| `tools/injury_model.py` | 1609 | T4 data-plane | 13.3% | 558 |
| `tools/kelly.py` | 895 | T0 money-path | 39.6% | 541 |
| `tools/clv_tracker.py` | 943 | T0 money-path | 45.7% | 512 |
| `tools/dk_scraper.py` | 1254 | T4 data-plane | 10.4% | 450 |
| `tools/knowledge_wiki.py` | 1350 | T3 epistemics | 46.5% | 433 |
| `tools/health.py` | 916 | T1 unattended-loop | 45.6% | 399 |
| `tools/regime.py` | 980 | T4 data-plane | 0.0% | 392 |
| `tools/dead_numbers.py` | 1250 | T4 data-plane | 22.2% | 389 |
| `tools/odds_api_io.py` | 1518 | T4 data-plane | 39.8% | 365 |
| `tools/arbitrage_scanner.py` | 1109 | T0 money-path | 68.1% | 354 |
| `tools/hermes_memory.py` | 767 | T3 epistemics | 28.7% | 328 |


## Full per-module table — every tools/, agp/, root module ≥300 raw lines

| module | lines | stmts | covered | cov % | named test | tier |
|---|---|---|---|---|---|---|
| `tools/autonomous.py` | 7955 | 3849 | 139 | 3.6% | **no** | T1 unattended-loop |
| `api.py` | 4685 | 2279 | 529 | 23.2% | **no** | T5 serving |
| `tools/backtest.py` | 4211 | 1714 | 589 | 34.4% | **no** | T2 gate |
| `tools/data_collector.py` | 3156 | 1486 | 161 | 10.8% | **no** | T4 data-plane |
| `tools/hypothesis.py` | 2848 | 1200 | 656 | 54.7% | yes | T2 gate |
| `tools/schema.py` | 1981 | 240 | 138 | 57.5% | **no** | T2 gate |
| `tools/line_monitor.py` | 1958 | 1004 | 80 | 8.0% | **no** | T4 data-plane |
| `orchestrator.py` | 1896 | 580 | 97 | 16.7% | **no** | T1 unattended-loop |
| `tools/hypothesis_generator.py` | 1684 | 436 | 169 | 38.8% | **no** | T2 gate |
| `tools/injury_model.py` | 1609 | 489 | 65 | 13.3% | **no** | T4 data-plane |
| `tools/golf_masters.py` | 1574 | 583 | 0 | 0.0% | **no** | other |
| `tools/market_psychology.py` | 1522 | 441 | 30 | 6.8% | **no** | other |
| `tools/odds_api_io.py` | 1518 | 623 | 248 | 39.8% | **no** | T4 data-plane |
| `tools/edge_scanner.py` | 1507 | 697 | 293 | 42.0% | **no** | T0 money-path |
| `tools/line_analysis.py` | 1485 | 496 | 28 | 5.6% | **no** | T4 data-plane |
| `tools/thesis_seeds.py` | 1462 | 78 | 70 | 89.7% | yes | other |
| `tools/pace_model.py` | 1405 | 429 | 0 | 0.0% | **no** | T4 data-plane |
| `tools/simulation.py` | 1369 | 507 | 233 | 46.0% | yes | other |
| `tools/knowledge_wiki.py` | 1350 | 499 | 232 | 46.5% | **no** | T3 epistemics |
| `tools/prop_scraper_free.py` | 1306 | 529 | 65 | 12.3% | yes | T4 data-plane |
| `tools/dk_scraper.py` | 1254 | 540 | 56 | 10.4% | **no** | T4 data-plane |
| `tools/bet_executor.py` | 1253 | 510 | 236 | 46.3% | **no** | T0 money-path |
| `tools/dead_numbers.py` | 1250 | 266 | 59 | 22.2% | **no** | T4 data-plane |
| `tools/environment.py` | 1250 | 377 | 0 | 0.0% | **no** | other |
| `tools/pipeline_integrity.py` | 1191 | 432 | 53 | 12.3% | **no** | T1 unattended-loop |
| `tools/temporal_analysis.py` | 1122 | 325 | 36 | 11.1% | **no** | other |
| `tools/arbitrage_scanner.py` | 1109 | 426 | 290 | 68.1% | **no** | T0 money-path |
| `tools/ml_features.py` | 1097 | 382 | 293 | 76.7% | yes | T2 gate |
| `tools/correlation.py` | 1079 | 290 | 31 | 10.7% | **no** | other |
| `tools/self_repair.py` | 1031 | 666 | 114 | 17.1% | **no** | T1 unattended-loop |
| `tools/order_reconciler.py` | 1008 | 484 | 403 | 83.3% | yes | T0 money-path |
| `tools/regime.py` | 980 | 358 | 0 | 0.0% | **no** | T4 data-plane |
| `tools/clv_tracker.py` | 943 | 317 | 145 | 45.7% | **no** | T0 money-path |
| `tools/health.py` | 916 | 408 | 186 | 45.6% | **no** | T1 unattended-loop |
| `tools/live_state.py` | 905 | 439 | 250 | 56.9% | **no** | T4 data-plane |
| `tools/kelly.py` | 895 | 250 | 99 | 39.6% | **no** | T0 money-path |
| `tools/news_ingestion.py` | 861 | 326 | 222 | 68.1% | yes | T4 data-plane |
| `inference.py` | 849 | 327 | 176 | 53.8% | **no** | T5 serving |
| `tools/tci_scraper.py` | 811 | 253 | 0 | 0.0% | **no** | T4 data-plane |
| `tools/embeddings.py` | 795 | 305 | 151 | 49.5% | **no** | T3 epistemics |
| `tools/hermes_memory.py` | 767 | 363 | 104 | 28.7% | **no** | T3 epistemics |
| `tools/bankroll_sim.py` | 739 | 306 | 237 | 77.5% | yes | T0 money-path |
| `tools/order_manager.py` | 735 | 293 | 254 | 86.7% | **no** | T0 money-path |
| `tools/followup_guard.py` | 733 | 262 | 187 | 71.4% | **no** | T1 unattended-loop |
| `tools/action_network_scraper.py` | 680 | 233 | 33 | 14.2% | **no** | T4 data-plane |
| `tools/telegram.py` | 680 | 353 | 40 | 11.3% | **no** | T5 serving |
| `tools/live_edges.py` | 652 | 191 | 164 | 85.9% | **no** | T0 money-path |
| `tools/boost_evaluator.py` | 649 | 171 | 121 | 70.8% | yes | T0 money-path |
| `tools/ml_classifier.py` | 649 | 271 | 0 | 0.0% | yes | T2 gate |
| `tools/sgp_scanner.py` | 647 | 236 | 196 | 83.1% | yes | T0 money-path |
| `tools/market_regime.py` | 644 | 252 | 222 | 88.1% | yes | T4 data-plane |
| `tools/cache_manager.py` | 639 | 233 | 32 | 13.7% | **no** | T4 data-plane |
| `tools/parlay_scanner.py` | 624 | 233 | 197 | 84.5% | yes | T0 money-path |
| `tools/edge_confidence.py` | 618 | 271 | 178 | 65.7% | yes | T2 gate |
| `tools/db_writer.py` | 589 | 275 | 222 | 80.7% | yes | T4 data-plane |
| `tools/odds_api.py` | 581 | 205 | 87 | 42.4% | yes | T4 data-plane |
| `tools/fanatics_scraper.py` | 574 | 268 | 204 | 76.1% | yes | T4 data-plane |
| `tools/dashboard.py` | 557 | 269 | 218 | 81.0% | yes | T5 serving |
| `tools/claude_code.py` | 556 | 224 | 113 | 50.4% | yes | T5 serving |
| `tools/historical_odds.py` | 539 | 223 | 73 | 32.7% | **no** | T4 data-plane |
| `tools/betmgm_scraper.py` | 530 | 247 | 33 | 13.4% | **no** | T4 data-plane |
| `tools/contextual_data.py` | 523 | 186 | 27 | 14.5% | **no** | T4 data-plane |
| `tools/local_cc_bridge.py` | 520 | 166 | 128 | 77.1% | yes | T5 serving |
| `tools/prop_resolver.py` | 500 | 192 | 143 | 74.5% | yes | T4 data-plane |
| `tools/sim.py` | 471 | 127 | 13 | 10.2% | **no** | other |
| `memory.py` | 453 | 160 | 91 | 56.9% | yes | T3 epistemics |
| `tools/work_queue.py` | 439 | 201 | 139 | 69.2% | yes | T1 unattended-loop |
| `tools/narrative_edge.py` | 436 | 146 | 0 | 0.0% | **no** | other |
| `tools/news_impact.py` | 425 | 146 | 117 | 80.1% | yes | T4 data-plane |
| `tools/learned_correlations.py` | 407 | 154 | 36 | 23.4% | **no** | other |
| `agp/__init__.py` | 399 | 193 | 184 | 95.3% | **no** | T3 epistemics |
| `tools/game_dates.py` | 385 | 73 | 67 | 91.8% | **no** | T4 data-plane |
| `tools/credentials.py` | 380 | 107 | 104 | 97.2% | yes | other |
| `tools/sgp_correlations.py` | 373 | 138 | 102 | 73.9% | yes | other |
| `tools/granger_causality.py` | 367 | 138 | 0 | 0.0% | **no** | other |
| `task_queue.py` | 367 | 181 | 103 | 56.9% | **no** | T1 unattended-loop |
| `tools/fanduel_scraper.py` | 365 | 160 | 21 | 13.1% | **no** | T4 data-plane |
| `tools/ml_backtest.py` | 360 | 169 | 0 | 0.0% | **no** | T2 gate |
| `tools/callisto_mcp_server.py` | 349 | 114 | 0 | 0.0% | **no** | T5 serving |
| `tools/prop_fair_value.py` | 319 | 122 | 97 | 79.5% | yes | T4 data-plane |
| `tools/devig.py` | 317 | 140 | 120 | 85.7% | yes | other |
| `tools/line_gaps.py` | 313 | 125 | 115 | 92.0% | yes | other |
| `tools/ingestion_tracking.py` | 301 | 130 | 111 | 85.4% | yes | T4 data-plane |


## Coverage that exists only via implementation-pinned or incidental tests (sampled)

- **`api.py` 23.2% is an illusion of import.** No test file imports `api` at
  all. Its executed lines are dominated by import-time constants; the only
  behavioural reach is two lazy imports (`tools/autonomous.py:3209`
  `from api import historical_fetcher`, `tools/line_monitor.py:1444`
  `from api import clv_tracker`). 4,685 lines of HTTP route handlers: ~zero
  behavioural coverage. VERIFIED via `coverage.json` executed-lines + grep.
- **`tests/test_full_system_audit.py` is not a test suite.** 0 `def test_`
  functions; a script-style checker that runs against a DB that does not exist
  on this machine (its own file coverage: 5.1%). It imports `kelly` and
  `embeddings` but contributes nothing measurable here. Its "PROOF" output
  should not be cited as test evidence.
- **`tests/test_claude_findings.py` (21 tests, 0 mocks) pins only tiny keyword
  classifiers** inside `autonomous.py`/`self_repair.py` — that is the entirety
  of why a 7,955-line unattended-loop monolith shows 3.6%: the tested surface
  is `_classify_*`-style helpers, not the loop.
- **Behaviour-vs-implementation ratio in the money-path sample: good.**
  `test_portfolio_sizing.py`, `test_drawdown_kill.py`, `test_bankroll_race.py`,
  `test_clv_units.py` contain zero `patch()/Mock()` usages and assert numeric
  outcomes (caps, signs, magnitudes, DB rows) — these pin behaviour, not
  implementation. Contrast `tests/test_backtest_e2e.py`, whose 11 failures are
  assertion-level (e.g. `props_scanned 0 >= 1`) — a suite currently red on
  behaviour, which is exactly what a suite is for; do not "fix" by weakening
  asserts (§5.4).
- **Named-but-hollow:** `tests/test_prop_scraper_free.py` exists; module
  coverage 12.3% — the test file exercises a sliver and the scraper mass is
  untested despite the name. Named test ≠ coverage.

## Reproduce

```bash
git checkout cartography/coverage-map
.venv/bin/python -m pytest -q --timeout=120 \
  --ignore=tests/test_ml_classifier.py --ignore=tests/test_ml_drift.py \
  --deselect tests/test_backtest_e2e.py::TestBacktestEndToEnd::test_full_pipeline \
  --deselect tests/test_backtest_e2e.py::TestBacktestEndToEnd::test_totals_resolution \
  --deselect tests/test_backtest_e2e.py::TestBacktestEndToEnd::test_h2h_resolution \
  --deselect tests/test_backtest_e2e.py::TestBacktestEndToEnd::test_spreads_push \
  --deselect tests/test_backtest_e2e.py::TestBacktestEndToEnd::test_away_team_wins_h2h \
  --deselect tests/test_backtest_e2e.py::TestBacktestEndToEnd::test_backtest_run_record \
  --deselect tests/test_backtest_e2e.py::TestPaperTradeResolution::test_resolve_spread_paper_trade \
  --deselect tests/test_backtest_e2e.py::TestPaperTradeResolution::test_resolve_totals_paper_trade \
  --deselect tests/test_backtest_e2e.py::TestPaperTradeResolution::test_resolve_h2h_paper_trade \
  --deselect tests/test_backtest_e2e.py::TestRecalculateRunStats::test_deferred_resolution_updates_run \
  --deselect tests/test_backtest_e2e.py::TestRecalculateRunStats::test_global_resolution_recalculates_all_stale_runs \
  --deselect tests/test_prop_scanner.py::TestPropScanner::test_finds_edge \
  --cov=tools --cov=agp --cov=. \
  --cov-report=json:coverage.json --cov-report=term-missing:skip-covered -p no:cacheprovider
# optional fidelity merge of the 12 failing bodies:
.venv/bin/python -m pytest -q --timeout=90 \
  tests/test_backtest_e2e.py tests/test_prop_scanner.py \
  -k "test_full_pipeline or test_totals_resolution or test_h2h_resolution or test_spreads_push or test_away_team_wins_h2h or test_backtest_run_record or paper_trade or test_deferred_resolution_updates_run or test_global_resolution_recalculates_all_stale_runs or test_finds_edge" \
  --cov=tools --cov=agp --cov=. --cov-append --cov-report= -p no:cacheprovider
.venv/bin/python -m coverage json -o coverage.json
```

Falsifier for this whole map: re-run the commands above on the workstation with
the real DB; modules gated on DB contents (`data_collector`, `line_monitor`,
`backtest` resolution paths) can only measure higher, never lower — coverage
here is a floor, not a ceiling.
