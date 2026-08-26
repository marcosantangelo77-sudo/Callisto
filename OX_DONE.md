# OX_DONE — split line_monitor into tools.lines

Branch: `cursor/ox-linemon-split-2ac0`

## What was done

`tools/line_monitor.py` (~1958 lines) was split into a new `tools/lines/`
package. `LineMonitor` import path and all legacy module-level names
(`MONITORED_SPORTS`, `WS_SPORTS`, thresholds, `_ws_update_to_snapshot`,
`_merge_delta_into_snapshot`, `_stamp_snapshot_fetched_at`) are unchanged.

### New modules

| File | Lines | Contents |
|---|---:|---|
| `tools/line_monitor.py` (after) | 1274 | LineMonitor class, loops, DB writes, CLV bridge; delegates to tools.lines |
| `tools/lines/__init__.py` | 8 | package docstring |
| `tools/lines/ingest.py` | 364 | WS sport mapping, WS/incremental → snapshot conversion, delta merge, fetched_at stamping, shared scraper-enrichment helper (DK/FD/MGM/Fanatics), free-snapshot merge, matchup keys |
| `tools/lines/edge_report.py` | 290 | implied-prob extraction, devig cross-book consensus, movement → +EV evaluation (`MovementEvaluator`), model-agreement gate |
| `tools/lines/movement.py` | 206 | significance filtering, `MovementRecorder`, `KLDivergenceTracker` (compute/cache/evict) |
| `tests/test_linemon_split.py` | 473 | 41 tests |

Before: 1958 lines in one file. After: 1274 + 868 extracted across three modules.

## Tests

```
/tmp/callisto-pytest/bin/python -m pytest tests/test_linemon_split.py -q
41 passed
```

Also verified green (untouched consumers): test_tier4_data_resolution,
test_ws_single_owner, test_full_system_audit, test_integration_e2e (17 passed),
and `import api` succeeds.

## Not touched

- No betting armed, no paper-signal widening, backtest.py untouched.
