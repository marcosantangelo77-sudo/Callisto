import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory", "callisto.db")
conn = sqlite3.connect(db)
cur = conn.cursor()

cur.execute("SELECT hypothesis_id, name, status, sport, market_type, edge_threshold, created_at FROM hypotheses WHERE name = 'mlb_road_streak_runline_dog_cover'")
cols = [d[0] for d in cur.description]
rows = cur.fetchall()
print('=== HYPOTHESIS ===')
for r in rows:
    for c, v in zip(cols, r):
        print(f'  {c}: {v}')

print()
print('=== SIGNAL EVENTS ===')
cur.execute("""SELECT e.event_id, e.game_date, e.book, e.side, e.line, e.book_odds_american,
    e.book_implied_prob, e.model_fair_prob, e.edge, e.ev_pct, e.kelly_fraction,
    e.actual_result, e.signal_generated
FROM backtest_events e
WHERE e.hypothesis_id IN (SELECT hypothesis_id FROM hypotheses WHERE name = 'mlb_road_streak_runline_dog_cover')
AND e.signal_generated = 1
ORDER BY e.edge DESC""")
cols2 = [d[0] for d in cur.description]
rows2 = cur.fetchall()
print(f'Total signals: {len(rows2)}')
for r in rows2:
    print(dict(zip(cols2, r)))

print()
print('=== BOOK DISTRIBUTION ===')
cur.execute("""SELECT e.book, COUNT(*) as cnt, AVG(e.edge) as avg_edge
FROM backtest_events e
WHERE e.hypothesis_id IN (SELECT hypothesis_id FROM hypotheses WHERE name = 'mlb_road_streak_runline_dog_cover')
AND e.signal_generated = 1
GROUP BY e.book ORDER BY avg_edge DESC""")
for r in cur.fetchall():
    print(f'  {r[0]}: {r[1]} signals, avg_edge={r[2]:.4f}')

print()
cur.execute("""SELECT COUNT(*) as total, COUNT(DISTINCT e.event_id) as distinct_events,
    COUNT(DISTINCT e.game_date) as distinct_dates
FROM backtest_events e
WHERE e.hypothesis_id IN (SELECT hypothesis_id FROM hypotheses WHERE name = 'mlb_road_streak_runline_dog_cover')
AND e.signal_generated = 1""")
r = cur.fetchone()
print(f'=== SIGNAL SUMMARY ===')
print(f'  total_signals={r[0]}, distinct_events={r[1]}, distinct_dates={r[2]}')

cur.execute("""SELECT COUNT(*) FROM backtest_events
WHERE hypothesis_id IN (SELECT hypothesis_id FROM hypotheses WHERE name = 'mlb_road_streak_runline_dog_cover')""")
print(f'  total_events={cur.fetchone()[0]}')

cur.execute("""SELECT actual_result, COUNT(*) as cnt
FROM backtest_events
WHERE hypothesis_id IN (SELECT hypothesis_id FROM hypotheses WHERE name = 'mlb_road_streak_runline_dog_cover')
AND signal_generated = 1
GROUP BY actual_result""")
for r in cur.fetchall():
    print(f'  {r[0] or "pending"}: {r[1]}')

conn.close()
