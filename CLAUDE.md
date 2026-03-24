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
# Method 1: Signal file (works always — watchdog picks it up in ≤15 seconds)
echo "code reload after commit $(git rev-parse --short HEAD)" > memory/restart_requested

# Method 2: HTTP endpoint (works when API is still responding)
curl -s -X POST http://localhost:8420/admin/restart
```
The watchdog (scripts/watchdog.py) runs as a separate process, checks the signal file every 15s, kills the API, and restarts it with new code. Do NOT skip this step — uncommitted code that isn't reloaded is invisible to the live system.

## Watchdog Architecture
The API auto-restart system has three layers:
1. **watchdog.py** — Python process that health-checks every 15s, restarts API with full error logging, never gives up (exponential backoff, not surrender)
2. **watchdog.bat** — Batch loop that restarts watchdog.py if IT crashes
3. **Windows Task Scheduler** — Starts watchdog.bat at login, restarts on failure (install via `scripts/install_watchdog.bat` as admin)

Logs: `logs/watchdog.log`, `logs/api_stderr_*.log`

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

## Key Rules
- Never bypass AGP for research — submit to /task
- Every session starts by syncing with Callisto state
- After code changes: commit, then trigger restart (signal file or /admin/restart)
- Commit all code changes autonomously (feedback_autonomous_commits)
- Don't ask permission — check state and act (feedback_no_permission_asking)
- Betting must be quantitative with live odds APIs (feedback_betting_strategy)
- Never build around unproven theses — backtest first (feedback_thesis_testing)
