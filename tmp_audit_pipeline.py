import sqlite3, json

db = sqlite3.connect('memory/callisto.db')
db.row_factory = sqlite3.Row

print('=== BACKTEST EVENTS PER HYPOTHESIS (top 15) ===')
rows = db.execute('''SELECT hypothesis_id, COUNT(*) as events,
    SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END) as signals,
    ROUND(AVG(edge),4) as avg_edge,
    ROUND(MAX(edge),4) as max_edge,
    COUNT(DISTINCT event_id) as unique_games
    FROM backtest_events GROUP BY hypothesis_id ORDER BY events DESC LIMIT 15''').fetchall()
for r in rows:
    print(f'  {r["hypothesis_id"][:12]}: {r["events"]}ev/{r["unique_games"]}games, {r["signals"]}sig, avg_edge={r["avg_edge"]}, max_edge={r["max_edge"]}')

print('\n=== EDGE DISTRIBUTION (top 30 positive) ===')
edges = db.execute('SELECT edge, hypothesis_id, event_id FROM backtest_events WHERE edge > 0 ORDER BY edge DESC LIMIT 30').fetchall()
for e in edges:
    print(f'  {e["edge"]:.4f} ({e["hypothesis_id"][:8]})')

print('\n=== EDGE PERCENTILES ===')
all_edges = db.execute('SELECT edge FROM backtest_events WHERE edge IS NOT NULL ORDER BY edge').fetchall()
edges_list = [e['edge'] for e in all_edges]
if edges_list:
    n = len(edges_list)
    print(f'  Total: {n}, Min: {edges_list[0]:.4f}, Max: {edges_list[-1]:.4f}')
    print(f'  p50: {edges_list[n//2]:.4f}, p75: {edges_list[int(n*0.75)]:.4f}, p90: {edges_list[int(n*0.9)]:.4f}, p95: {edges_list[int(n*0.95)]:.4f}')

print('\n=== THRESHOLD vs MAX EDGE (top hypotheses) ===')
hyps = db.execute('''SELECT h.hypothesis_id, h.name, h.edge_threshold, COUNT(be.id) as events,
    MAX(be.edge) as max_edge, ROUND(AVG(be.edge),4) as avg_edge,
    SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals
    FROM hypotheses h JOIN backtest_events be ON h.hypothesis_id=be.hypothesis_id
    GROUP BY h.hypothesis_id ORDER BY events DESC LIMIT 15''').fetchall()
for h in hyps:
    blocked = h['max_edge'] is not None and h['edge_threshold'] is not None and h['max_edge'] < h['edge_threshold']
    status = 'BLOCKED' if blocked else 'ok'
    print(f'  {h["name"][:50]}: thresh={h["edge_threshold"]}, max={h["max_edge"]:.4f}, avg={h["avg_edge"]}, sig={h["signals"]}, {status}')

print('\n=== HYPOTHESIS STATUS COUNTS ===')
statuses = db.execute('SELECT status, COUNT(*) as cnt FROM hypotheses GROUP BY status').fetchall()
for s in statuses:
    print(f'  {s["status"]}: {s["cnt"]}')

print('\n=== SIGNAL EVENTS DETAIL ===')
sigs = db.execute('''SELECT be.hypothesis_id, h.name, be.edge, be.event_id, be.signal_generated
    FROM backtest_events be JOIN hypotheses h ON h.hypothesis_id=be.hypothesis_id
    WHERE be.signal_generated=1 ORDER BY be.edge DESC''').fetchall()
print(f'  Total signals: {len(sigs)}')
for s in sigs[:20]:
    print(f'  {s["name"][:40]}: edge={s["edge"]:.4f}')

print('\n=== IDENTICAL EVENT SETS CHECK ===')
# Get hypothesis->event_ids mapping
hyp_events = db.execute('''SELECT hypothesis_id, GROUP_CONCAT(event_id) as events
    FROM (SELECT hypothesis_id, event_id FROM backtest_events ORDER BY event_id)
    GROUP BY hypothesis_id HAVING COUNT(*) >= 20''').fetchall()
event_map = {}
for h in hyp_events:
    key = h['events']
    if key not in event_map:
        event_map[key] = []
    event_map[key].append(h['hypothesis_id'])
for key, hids in event_map.items():
    if len(hids) > 1:
        names = []
        for hid in hids:
            n = db.execute('SELECT name FROM hypotheses WHERE hypothesis_id=?', (hid,)).fetchone()
            names.append(n['name'] if n else hid[:12])
        print(f'  IDENTICAL ({len(key.split(","))} events): {" | ".join(n[:40] for n in names)}')

print('\n=== RESOLVED vs UNRESOLVED ===')
resolved = db.execute("SELECT COUNT(*) as cnt FROM backtest_events WHERE actual_result IS NOT NULL AND actual_result != ''").fetchone()
unresolved = db.execute("SELECT COUNT(*) as cnt FROM backtest_events WHERE actual_result IS NULL OR actual_result = ''").fetchone()
print(f'  Resolved: {resolved["cnt"]}, Unresolved: {unresolved["cnt"]}')

print('\n=== HYPOTHESES WITH 50+ EVENTS AND 0 SIGNALS (reject candidates) ===')
rejects = db.execute('''SELECT h.hypothesis_id, h.name, h.status, COUNT(be.id) as events,
    SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals,
    ROUND(MAX(be.edge),4) as max_edge, h.edge_threshold
    FROM hypotheses h JOIN backtest_events be ON h.hypothesis_id=be.hypothesis_id
    WHERE h.status='backtesting'
    GROUP BY h.hypothesis_id HAVING events >= 50 AND signals = 0
    ORDER BY events DESC''').fetchall()
for r in rejects:
    print(f'  {r["hypothesis_id"][:12]} {r["name"][:45]}: {r["events"]}ev, max_edge={r["max_edge"]}, thresh={r["edge_threshold"]}')

db.close()
