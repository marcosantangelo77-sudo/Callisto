# Ingestion Triage — 2026-04-23

Point-in-time audit against `data/callisto.db::ingestion_runs` as of UTC ~23:25.

## The task's "7 genuinely broken" claim vs reality

The task brief flagged 7 sources as genuinely broken. Actual state from the live
ledger (most-recent finished run per source, compared against SLA defaults):

| Source | Claimed | Observed age (s) | SLA | Verdict |
|---|---|---:|---:|---|
| espn.scoreboard.baseball_mlb | broken | 1349 | 900 | WARN only. Last run status=`ok`, 15 rows. Not broken — routine jitter. |
| espn.boxscore.baseball_mlb | broken | 1348 | 1800 | OK. status=`partial` (0 rows) — evening scan before games complete. Working as designed. |
| espn.pbp.baseball_mlb | broken | 1347 | 3600 | OK. Same as above. |
| nhl_api.shots | broken | 1324 | 3600 | OK. status=`ok`, rows ingested. Not broken. |
| odds_api_io.v3.odds.updated | broken | 955 | 600 | **REAL BUG**. 200 attempts / 0 successes. HTTP 400 "Missing bookmaker parameter". Fixed in this PR. |
| odds_api_io.v3.movements.snapshot | broken | 44216 | 900 | **REAL BUG**. 1078 partial / 0 ok. Stopped being called 12h ago because backtest path stalled. Now will fan out with ML-only per the movements fix below. |
| odds_api_io.v3.movements.ML | broken | 44216 | 900 | **REAL BUG**. HTTP 404 "No data found for market ML with line 0" — legitimate API response for archived events (movement history window exceeded). Noise suppressed by the non-ML short-circuit in this PR. |

Additional critical-tier source discovered in the scan:

| Source | Age (s) | SLA | Verdict |
|---|---:|---:|---|
| odds_api_io.v3.movements.Spread | 44216 | 900 | **REAL BUG**. 15,843 HTTP 400 "Missing marketLine" rows. Short-circuited in this PR. |
| odds_api_io.v3.movements.Totals | 44216 | 900 | Same as Spread. |
| fanatics.odds.golf_pga | 66921 | 7200 | Out-of-scope for this PR (separate scraper), recommend follow-up. |

## Root causes

### odds-api.io /odds/updated (HTTP 400)
API now requires BOTH `sport` and `bookmaker` query params. Our code made them
optional. Additionally the `sport` value must be the DISPLAY NAME
("Basketball", "Ice Hockey") not the slug ("basketball", "ice-hockey") —
documented inconsistency with every other endpoint on the same API. This
endpoint returned HTTP 400 on 100% of calls for ≥20 hours — the oldest
ingestion_runs row for this source is also the first failure.

Fix: add slug→display map, default `bookmaker="DraftKings"`, return a sentinel
if `sport` is omitted (so we stop writing a `failed` row for a call we know
will fail at the API).

### odds-api.io /odds/movements (HTTP 400 / 404)
API now requires `marketLine` for every market except ML. Our code never
passed it. Result: 31,687 `failed` rows for Spread+Totals (HTTP 400 "Missing
marketLine"), 15,843 `failed` rows for ML (HTTP 404 "No data found for market
ML with line 0"). The ML 404s are actually legitimate — movements history is
only retained for a rolling window of ~recent events, and the backtest fan-out
was asking about archived events.

Fix:
1. Plumb `market_line` through `get_odds_movements`.
2. For non-ML markets without a `market_line`, return an error sentinel
   immediately — no wire call, no `failed` row.
3. In `get_historical_snapshot` fan-out, skip non-ML markets explicitly (they
   fall through to the existing `closing_fallback` path cleanly; we don't have
   a `market_line` at that call site).

### ESPN MLB + NHL shots (not stale)
Per the ledger, these ran successfully within the last ~25 minutes. They
appear "stale" only in flavors of the health probe that look for `status="ok"`
specifically — the decorator tags runs `partial` when the normalized return
value has zero rows (by design, to let the health check distinguish "empty
day" from "never ran"). For ESPN PBP/boxscore in the evening before games
complete, `partial` is the correct tag.

The task brief suggested "tag status=failed instead of partial when upstream
returns 200 but no schedule for the day AND no events" — rejected because:
- Failure-tagging an empty day turns routine morning pre-game scans into
  alerts.
- The health-check query already treats `partial` as healthy when within SLA
  (tools/health.py lines 787-788). The stale-critical tier trips on age, not
  on status — so a `partial` that keeps hitting the endpoint never trips.
- The actual problem was `odds_api_io`, not ESPN.

### prop_resolver.backtest_events (not stale)
Most-recent finished run 17 min ago, status=`partial` (zero rows to resolve).
`prop_resolution_loop` IS wired in `api.py` lifespan (line 1030). Age 1007s <
SLA 7200s. Healthy.

## Verification commands (user-runnable)

After deploying this branch and restarting the API, observe the next 5 minutes
of ingestion_runs for the affected sources:

```bash
# Should show status='ok' with non-zero rows after 60-90s of runtime:
sqlite3 memory/callisto.db "
SELECT source, status, started_at, rows_ingested, SUBSTR(error_message,1,80)
FROM ingestion_runs
WHERE source = 'odds_api_io.v3.odds.updated'
ORDER BY id DESC LIMIT 5;"

# Should show fewer/no 'Missing marketLine' failures (non-ML no longer hits
# the wire unless the caller supplies market_line):
sqlite3 memory/callisto.db "
SELECT status, COUNT(*), SUBSTR(error_message,1,60) AS err
FROM ingestion_runs
WHERE source LIKE 'odds_api_io.v3.movements.%'
  AND started_at > datetime('now', '-5 minutes')
GROUP BY status, err
ORDER BY 2 DESC;"

# /health should report the two odds_api_io sources moving to healthy:
curl -s http://localhost:8420/health | jq '.subsystems.data_collector // .last_checks.data_collector'
```

## Follow-ups (not in this PR)

- `fanatics.odds.golf_pga` 18.5h stale — separate scraper issue, not related
  to odds-api.io. Scope to a different branch.
- Multi-book incremental: `_incremental_loop` in `tools/line_monitor.py`
  currently only polls the default (DraftKings) for `/odds/updated`. If we
  want multi-book WS gap-filling, it needs an outer loop over anchor books.
  Credits: O(sports × books × interval), currently O(sports × interval).
- `get_historical_snapshot`: the Spread/Totals paths are now pure
  `closing_fallback`. If we want pre-commence Spread/Totals, the caller needs
  to discover the line from an earlier `/odds` call and pass it as
  `market_line` into a follow-up movements call — extra credit cost.
