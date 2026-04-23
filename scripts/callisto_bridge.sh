#!/usr/bin/env bash
# Callisto Bridge — syncs Claude Code session with live Callisto instance
# Called automatically on session start via Claude Code hooks

CALLISTO_URL="${CALLISTO_URL:-http://localhost:8420}"

# Check if Callisto is running
health=$(curl -s --connect-timeout 3 "$CALLISTO_URL/health" 2>/dev/null)
if [ $? -ne 0 ] || [ -z "$health" ]; then
    echo "CALLISTO_STATUS=OFFLINE"
    exit 0
fi

echo "=== CALLISTO LIVE STATE ==="
echo ""

# Health summary
echo "--- System Health ---"
echo "$health" | python3 -c "
import sys, json
try:
    h = json.load(sys.stdin)
    agents = h.get('agents', {})
    for name, info in agents.items():
        print(f'  {name}: {info.get(\"status\", \"unknown\")} ({info.get(\"model\", \"?\")})')
    cc = h.get('claude_code', {})
    usage = cc.get('usage', {})
    print(f'  claude_code: {\"available\" if cc.get(\"available\") else \"cooldown\"} ({usage.get(\"calls_this_window\", 0)}/{usage.get(\"max_calls_per_hour\", 0)} calls)')
    odds = h.get('odds_api', {})
    print(f'  odds_api: {odds.get(\"remaining\", \"?\")} credits remaining')
    lm = h.get('line_monitor', {})
    print(f'  line_monitor: {\"running\" if lm.get(\"running\") else \"stopped\"} | sports: {lm.get(\"monitored_sports\", [])}')
    auto = h.get('autonomous', {})
    print(f'  autonomous: {\"running\" if auto.get(\"running\") else \"stopped\"} | sessions: {auto.get(\"sessions_run\", 0)} | alerts: {auto.get(\"alerts_sent\", 0)}')
except Exception as e:
    print(f'  (parse error: {e})')
" 2>/dev/null

echo ""

# Recent tasks
echo "--- Recent Tasks ---"
tasks=$(curl -s --connect-timeout 3 "$CALLISTO_URL/tasks?limit=5" 2>/dev/null)
echo "$tasks" | python3 -c "
import sys, json
try:
    data = json.load(sys.stdin)
    for t in data.get('tasks', []):
        status = t['status']
        q = t['query'][:80] + ('...' if len(t['query']) > 80 else '')
        print(f'  [{status}] #{t[\"task_id\"]}: {q}')
    if not data.get('tasks'):
        print('  (no recent tasks)')
except Exception as e:
    print(f'  (parse error: {e})')
" 2>/dev/null

echo ""

# Full system status (hypotheses, embeddings, research)
echo "--- Research Status ---"
full=$(curl -s --connect-timeout 3 "$CALLISTO_URL/system/full-status" 2>/dev/null)
echo "$full" | python3 -c "
import sys, json
try:
    s = json.load(sys.stdin)
    rl = s.get('research_loop', {})
    if rl:
        print(f'  research_loop: cycles={rl.get(\"cycles\", 0)} hypotheses_gen={rl.get(\"hypotheses_generated\", 0)} backtests={rl.get(\"backtests_run\", 0)} promotions={rl.get(\"promotions\", 0)}')
    hyp = s.get('hypotheses', {})
    if hyp:
        print(f'  hypotheses: {hyp.get(\"total\", 0)} total | {hyp.get(\"draft\", 0)} draft | {hyp.get(\"backtesting\", 0)} backtesting | {hyp.get(\"paper_trading\", 0)} paper | {hyp.get(\"live\", 0)} live | {hyp.get(\"rejected\", 0)} rejected')
    emb = s.get('embeddings', {})
    if emb:
        for coll, count in emb.items():
            print(f'  embeddings/{coll}: {count} vectors')
    data = s.get('data', {})
    if data:
        print(f'  data: {data.get(\"game_contexts\", 0)} games | {data.get(\"player_stats\", 0)} player stats')
except Exception as e:
    print(f'  (parse error: {e})')
" 2>/dev/null

echo ""
echo "=== END CALLISTO STATE ==="
