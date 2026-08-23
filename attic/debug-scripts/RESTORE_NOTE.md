# RESTORE NOTE — attic/debug-scripts/

Quarantined 2026-08-23 from repo root (branch `build/cli-front-door`).

These nine one-off SQL/debug scripts were session debris from earlier
conversations (ARCHITECTURE_MAP.md §1.3 lists all nine as VERIFIED orphans:
fan-in 0, no test, no script, no launcher consumer):

    callisto_query.py   callisto_query2.py   query_bt.py
    query_hyps.py       query_hyps_debug.py  query_pipeline.py
    run_query.py        check_nba_events.py  analysis.py

Their overlapping reports are covered by `python callisto.py status`
(lifecycle counts, top backtesting hypotheses, recent rejections, signal
events) and `python callisto.py doctor`.

Verified before quarantine: grep across tests/, tools/, agp/, scripts/,
api.py, orchestrator.py, inference.py, task_queue.py and all shell/ps1/bat
launchers finds zero references to any of these module names. Nothing
imports them; they were only ever run by hand.

To restore one: `git mv attic/debug-scripts/<name>.py .` — no import-path
changes are needed since they import only stdlib sqlite3.
