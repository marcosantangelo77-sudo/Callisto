# Callisto Autonomous Session Log
## Session: 2026-03-31 ~04:30 UTC

### Directive
Full autonomy from Marco. Objective: 10% efficiency improvement. Focus on structural issues, not surface patches.

### Starting State
- 3441 hypotheses: 176 draft, 30 backtesting, 0 paper_trading, 0 live, 3121 rejected, 114 retired
- 0 paper trades EVER generated
- 0 live hypotheses EVER sustained
- 96.5% rejection rate
- Best candidate: mlb_day_after_extra_innings_f5_under (21 signals, avg_edge -1.17%)
- Claude: available, 45/hr budget, Apriel 1.6 15B as #1 local fallback
- WAL: 13MB (fixed from 22GB), DB locks: all wrapped with retry

### Work Queue
1. [x] Diagnose why avg_edge is negative across ALL hypotheses
2. [ ] Fix duplicate NBA event sets (8 hypotheses, identical 837 events)
3. [x] Deep-dive MLB extra innings hypothesis  
4. [x] Audit devig methodology for systematic bias
5. [ ] Verify Apriel integration produces output
6. [ ] Optimize Claude call batching
7. [ ] Challenge evaluation thresholds
8. [ ] **CRITICAL** Fix stale backtest_runs stats blocking promotion
9. [ ] Update backtest_runs aggregates from backtest_events

---

### Finding 1: Edge Distribution Is Correct (04:35 UTC)
The negative avg_edge (-2.4%) across all events IS the vig. This is expected and correct.
- 13,406 events with negative edge (book has vig advantage)
- 143 events with positive edge (real signals)
- Signal rate: ~1.05% of all events
- The devig methodology is NOT biased — it correctly identifies the vig

### Finding 2: Signals ARE Winning (04:40 UTC)
Global signal win rate: 51.77% (1552W/1446L) — beats non-signal 50.95%.
Active backtesting hypotheses have STRONG results:
- **mlb_day_after_extra_innings_f5_under: 12W-3L (80%)**
- **nhl_elite_b2b_road_dog_depth_value: 8W-1L (89%)**
- NBA cluster (8 hypotheses): 5W-2L each (71%)

### Finding 3: CRITICAL — backtest_runs Stats Are Stale (04:45 UTC)
The promotion pipeline checks backtest_runs for stats, but this table was populated
at original backtest time. Since then:
- Retroactive signal updates changed signal_generated flags
- Game result resolution added actual_result values
- The backtest_runs.actual_win/loss/hit_rate/p_value are all WRONG
- MLB shows 1W-0L in backtest_runs but 12W-3L in backtest_events
- This is THE REASON nothing promotes — the stats the gate checks are stale

**Root cause**: `run_backtest()` writes stats to backtest_runs at completion (line 597-602),
but `resolve_from_game_results()` updates backtest_events without recomputing backtest_runs.

**FIX DEPLOYED (e519b68)**: Added `recalculate_all_active_runs()` to BacktestEngine.
Called at the start of every evaluate phase, right after game result resolution.
This ensures promotion gates always see fresh stats.

### Fix 4: Stale Backtest Stats (04:55 UTC) — DEPLOYED
- Commit: e519b68
- Added `BacktestEngine.recalculate_all_active_runs()` — loops all active runs, recomputes W/L/hit_rate
- Wired into `_phase_evaluate()` before promotion checks
- **Expected impact**: MLB (12W-3L), NHL (8W-1L) should now pass promotion gates on next evaluate cycle

### Status Check (05:20 UTC)
- Memory: 227.9 MB (was 631.7 MB), growth rate: **0.0 MB/hr** (was 98.4 MB/hr) 
- Draft: 229 (was 176) — 53 new hypotheses generated! Apriel+ladder producing
- Backtesting: 31, Paper trading: 0, Live: 0
- Duplicate NBA hypotheses auto-rejected by cleanup sweeps
- Cycle 1 still running (long cycle, 12 phases with timeouts)
- Waiting for cycle 2 evaluate phase to trigger stats recompute + promotion

### Status Check (05:10 UTC)
- Cycle 1 complete, waiting for cycle 2 evaluate phase
- NHL p_value=0.038 < 0.10 gate — promotion candidate once stats recompute runs
- MLB signals confirmed 12W-3L-3push (80% win rate) in backtest_events
- NBA 8 duplicate hypotheses confirmed — identical event sets, needs dedup
- System healthy, Claude available (45/hr), spinning_detected=false

### Commits This Session
| Time | Commit | Description |
|------|--------|-------------|
| 03:30 | ccdec64 | Auto-reject untestable drafts + line monitor DB locks |
| 03:45 | 0a7625d | WAL checkpoint, memory caps, edge thresholds, prop backtesting |
| 04:00 | 77cca01 | DB lock audit — all writes wrapped with retry |
| 04:15 | 2a10c11 | E2E audit fixes — anti-predictive sweep, Claude budget |
| 04:30 | b3ea11f | Apriel 1.6 15B wired into model ladder + local brain |
| 04:55 | e519b68 | CRITICAL: Fix stale backtest_runs blocking all promotions |
| ~05:00 | 873765e | Callisto self-repair: identical event set bug (regex+null game_filters) |
| 05:30 | 6ad585c | Fix stats race condition: signals_generated recount + false rejection requeue |
| 05:40 | 318d560 | Hotfix: optimize stale signal requeue query (startup stall) |
| 05:45 | 85ac422 | Fix requeue bug: cursor.fetchall() overwrite. NHL restored to backtesting |

### Fix 5: Hotfix stale signal requeue query (05:40 UTC) — DEPLOYED
- Commit: 318d560
- Correlated subquery on 3000+ rows was stalling startup. Split into two-step.

### Fix 6: Requeue cursor.fetchall() overwrite bug (05:45 UTC) — DEPLOYED  
- Commit: 85ac422
- The requeue function had a duplicate `rows = await cursor.fetchall()` that overwrote
  the populated rows list. NHL hypothesis finally restored to backtesting.

### Fix 7: Error Patterns Institutional Memory (06:15 UTC) — DEPLOYED
- Commit: 867771a
- Created memory/error_patterns.md with all known failure modes
- Injected into Claude's interpret and diagnose prompts
- Based on Boris Cherny's self-correcting CLAUDE.md technique

### Callisto Self-Repair Commits (system autonomously committed):
- 873765e — Identical event set bug (regex + null game_filters)
- 26a01ba — Context filter bypass: 19 missing patterns

### Finding 4: NHL Hypothesis Restored (05:47 UTC)
- `nhl_elite_b2b_road_dog_depth_value` back in backtesting with 9 signals (8W-1L)
- 3 total hypotheses requeued from false "0 signals" rejections
- Race condition chain: startup → retroactive signal update → evaluate sees stale stats → rejects
- Now fixed: requeue at startup + signals_generated recount in evaluate phase

---

