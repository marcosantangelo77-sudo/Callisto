import sqlite3

db_path = 'memory/callisto.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

print("=" * 100)
print("NBA SIGNAL RATE DIAGNOSIS")
print("=" * 100)

print("\n1. DEVIG RELIABILITY: MIN_BOOKS_FOR_SIGNAL = 4")
print("-" * 100)

cur.execute("""
SELECT 
    COUNT(CASE WHEN json_extract(model_factors, '$.books_used') >= 4 THEN 1 END) as at_4,
    COUNT(CASE WHEN json_extract(model_factors, '$.books_used') < 4 THEN 1 END) as below_4
FROM backtest_events be
JOIN hypotheses h ON be.hypothesis_id = h.hypothesis_id
WHERE h.sport = 'basketball_nba' AND h.market_type IN ('spreads', 'h2h')
""")

row = cur.fetchone()
at_4 = row['at_4']
below_4 = row['below_4']
total = at_4 + below_4

print(f"Events: {at_4} >= 4 books ({at_4/total*100:.1f}%), {below_4} < 4 books ({below_4/total*100:.1f}%)")
print(f"Location: tools/backtest.py:2302")
print(f"Problem: {below_4} events filtered out - min 4 non-target books required for signal")

print("\n2. SIGNAL THRESHOLD vs EDGE DISTRIBUTION")
print("-" * 100)

cur.execute("""
SELECT 
    ROUND(MAX(edge), 4) as max_edge,
    ROUND(AVG(CASE WHEN edge > 0 THEN edge END), 4) as avg_pos_edge,
    COUNT(CASE WHEN edge > 0 THEN 1 END) as positive_count
FROM backtest_events WHERE (SELECT COUNT(*) FROM hypotheses h WHERE h.hypothesis_id = backtest_events.hypothesis_id AND h.sport = 'basketball_nba') > 0
""")

row = cur.fetchone()
max_e = row['max_edge']
avg_pos = row['avg_pos_edge']
pos_count = row['positive_count']

print(f"Max edge: {max_e:.4f} ({max_e*100:.2f}%)")
print(f"Avg positive edge: {avg_pos:.4f} ({avg_pos*100:.2f}%)")
print(f"Positive edges: {pos_count}")

cur.execute("""
SELECT edge_threshold, COUNT(*) as count
FROM hypotheses
WHERE sport = 'basketball_nba' AND market_type IN ('spreads', 'h2h')
GROUP BY edge_threshold ORDER BY edge_threshold
""")

print("\nThresholds in use:")
for r in cur.fetchall():
    t = r[0]
    c = r[1]
    print(f"  {t:.1%}: {c} hypotheses")

print("\n3. GAME_FILTERS COVERAGE")
print("-" * 100)

cur.execute("""
SELECT 
    SUM(CASE WHEN model_config LIKE '%game_filters%' THEN 1 ELSE 0 END) as with_filters,
    COUNT(*) - SUM(CASE WHEN model_config LIKE '%game_filters%' THEN 1 ELSE 0 END) as no_filters
FROM hypotheses
WHERE sport = 'basketball_nba' AND market_type IN ('spreads', 'h2h')
""")

row = cur.fetchone()
print(f"With game_filters: {row[0]}")
print(f"Without game_filters: {row[1]}")
print(f"Location: tools/backtest.py:1654-1740")

print("\n4. RECENT CHANGES")
print("-" * 100)
print("Commit 0a7625d (2026-03-30): Lowered all thresholds > 0.3% to 0.3%")
print("Reason: Capture 0.3-0.5% edges previously filtered")
print("Result: Signal rate unchanged at 1-2% on 30-60 events")

conn.close()
