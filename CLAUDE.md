# Callisto — Autonomous Multi-Agent System

## On Session Start
Every new Claude Code session in this project MUST:
1. Check if Callisto is running: `curl -s http://localhost:8420/health`
2. Pull current state: `curl -s http://localhost:8420/system/full-status`
3. Pull recent tasks: `curl -s http://localhost:8420/tasks?limit=5`
4. Use this context to understand what the system is currently doing before taking action

## Routing Research Through Callisto
ALL research, analysis, and investigation queries MUST be submitted to the live Callisto instance via POST /task:
```bash
curl -X POST http://localhost:8420/task -H "Content-Type: application/json" -d '{"query": "...", "priority": 1}'
```
Do NOT perform ad-hoc research with sub-agents when Callisto is running. The AGP protocol exists to enforce evidence standards, confidence tiers, contradiction checks, and cryptographic sealing.

## Syncing Conversation Context
When a conversation contains insights, decisions, or research that the autonomous system should know about, submit them:
```bash
curl -X POST http://localhost:8420/context/sync -H "Content-Type: application/json" -d '{"session_summary": "...", "actionable_queries": ["..."]}'
```

## After Code Changes — Trigger Restart
When you modify Callisto source files (tools/, api.py, orchestrator.py, etc.), the running process needs to reload. Do this EVERY TIME after committing code changes:
```bash
# Method 1: Signal file (works always — API + watchdog both pick it up in ≤15s)
bash scripts/request_restart.sh "code reload after commit $(git rev-parse --short HEAD)"

# Method 2: HTTP endpoint (loopback-allowed; no token required on localhost)
curl -s -X POST "http://localhost:8420/admin/restart?confirm=YES"
```
State files live OFF OneDrive to avoid oplock-induced freezes — see `tools/state_paths.py`. Signal file is at `%LOCALAPPDATA%\Callisto\restart_requested` on Windows (or `~/.local/state/callisto/restart_requested` on Unix; override with `CALLISTO_STATE_DIR`). Both the API's `restart_signal_watcher` and `scripts/watchdog.py` poll this file every ~10s.

## Watchdog Architecture
The API auto-restart system has three layers:
1. **watchdog.py** — Python process that health-checks every 15s, writes a heartbeat each loop, restarts API with full error logging, never gives up (exponential backoff, not surrender). Self-recovers from a frozen primary: on startup, if an existing watchdog's heartbeat is >90s stale it evicts and takes over.
2. **watchdog.bat** — Batch loop that restarts watchdog.py if IT crashes
3. **Windows Task Scheduler** — Starts watchdog.bat at login, restarts on failure (install via `scripts/install_watchdog.bat` as admin)

State files (off OneDrive, `$STATE_DIR = %LOCALAPPDATA%\Callisto` by default):
- `watchdog.pid`, `watchdog.lock`, `watchdog_heartbeat.json`, `restart_requested`, `logs/watchdog.log`

API logs stay on OneDrive for cross-machine diagnostics: `logs/api_stdout_*.log`, `logs/api_stderr_*.log`.

## API Quick Reference (localhost:8420)
- `GET /health` — agent status, odds credits, monitors
- `GET /system/full-status` — everything: hypotheses, research loop, embeddings, data
- `GET /tasks?limit=N` — recent task queue
- `GET /task/{id}` — specific task result
- `POST /task` — submit research query to AGP pipeline
- `POST /context/sync` — push session context to Callisto
- `POST /admin/restart` — graceful restart (watchdog brings it back with new code)
- `GET /odds/edges` — current cross-book edges
- `GET /odds/opportunities` — +EV opportunities
- `GET /odds/movements` — recent line movements
- `GET /world/{domain}` — query domain memory (FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, GENERAL)

## Verifying the Local-Only Kill Switch (E2E)
`CALLISTO_LOCAL_ONLY=1` must be provably safe before it is ever used as
the main operating mode. To prove this end-to-end:
```bash
bash scripts/local_only_e2e.sh
# or directly:
python scripts/local_only_e2e.py --port 8421
```
The script spins up `api.py` as an isolated subprocess (temp
`CALLISTO_STATE_DIR`, temp DB, port 8421) with the kill switch on,
exercises every loop and subsystem (task pipeline, research/collect +
generate, backtest, odds snapshot, odds reads, metrics), stops the
process gracefully, then grep-audits the captured logs for any
`anthropic.com` URL or `claude` subprocess spawn marker. On PASS it
writes an ISO-8601 timestamp to `$STATE_DIR/local_only_verified_at`,
which is surfaced at `GET /health#local_only_verified_at`. The pytest
wrapper is `tests/test_local_only_e2e.py` (marker: `slow`; honors
`CALLISTO_SKIP_E2E=1`). Exit 0 = PASS; non-zero = any leak, error, or
subsystem regression.

## Key Rules
- Never bypass AGP for research — submit to /task
- Every session starts by syncing with Callisto state
- After code changes: commit, then trigger restart (signal file or /admin/restart)
- Commit all code changes autonomously (feedback_autonomous_commits)
- Don't ask permission — check state and act (feedback_no_permission_asking)
- Betting must be quantitative with live odds APIs (feedback_betting_strategy)
- Never build around unproven theses — backtest first (feedback_thesis_testing)
