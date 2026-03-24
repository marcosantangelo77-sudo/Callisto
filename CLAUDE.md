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

## API Quick Reference (localhost:8420)
- `GET /health` — agent status, odds credits, monitors
- `GET /system/full-status` — everything: hypotheses, research loop, embeddings, data
- `GET /tasks?limit=N` — recent task queue
- `GET /task/{id}` — specific task result
- `POST /task` — submit research query to AGP pipeline
- `POST /context/sync` — push session context to Callisto
- `GET /odds/edges` — current cross-book edges
- `GET /odds/opportunities` — +EV opportunities
- `GET /odds/movements` — recent line movements
- `GET /world/{domain}` — query domain memory (FINANCIAL, TECHNICAL, SIGNAL, SYNTHESIS, GENERAL)

## Key Rules
- Never bypass AGP for research — submit to /task
- Every session starts by syncing with Callisto state
- Commit all code changes autonomously (feedback_autonomous_commits)
- Don't ask permission — check state and act (feedback_no_permission_asking)
- Betting must be quantitative with live odds APIs (feedback_betting_strategy)
- Never build around unproven theses — backtest first (feedback_thesis_testing)
