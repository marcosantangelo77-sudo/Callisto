import sqlite3, json

conn = sqlite3.connect('data/callisto.db')
conn.row_factory = sqlite3.Row

print('=== HYPOTHESES IN BACKTESTING ===')
rows = conn.execute("""
    SELECT id, name, sport, market_type,
           json_extract(stats, '$.total_events') as events,
           json_extract(stats, '$.total_signals') as signals,
           json_extract(stats, '$.avg_edge') as avg_edge,
           json_extract(stats, '$.win_rate') as win_rate,
           json_extract(stats, '$.p_value') as p_value,
           json_extract(stats, '$.brier_score') as brier,
           json_extract(stats, '$.ic') as ic,
           created_at, updated_at
    FROM hypotheses
    WHERE status = 'backtesting'
    ORDER BY json_extract(stats, '$.total_signals') DESC
    LIMIT 25
""").fetchall()
for r in rows:
    print(f"  {r['name']}: {r['signals'] or 0} sig / {r['events'] or 0} ev, edge={r['avg_edge']}, p={r['p_value']}, brier={r['brier']}, ic={r['ic']}")

print()
print('=== RECENT REJECTIONS (last 10) ===')
rows = conn.execute("""
    SELECT name, sport,
           json_extract(stats, '$.total_events') as events,
           json_extract(stats, '$.total_signals') as signals,
           json_extract(stats, '$.rejection_reason') as reason,
           updated_at
    FROM hypotheses
    WHERE status = 'rejected'
    ORDER BY updated_at DESC
    LIMIT 10
""").fetchall()
for r in rows:
    print(f"  {r['name']}: {r['signals'] or 0}/{r['events'] or 0}, reason={r['reason']}, at={r['updated_at']}")

print()
print('=== PROMOTION GATES CHECK ===')
rows = conn.execute("""
    SELECT name,
           json_extract(stats, '$.total_events') as events,
           json_extract(stats, '$.total_signals') as signals,
           json_extract(stats, '$.avg_edge') as avg_edge,
           json_extract(stats, '$.win_rate') as win_rate,
           json_extract(stats, '$.p_value') as p_value,
           json_extract(stats, '$.brier_score') as brier,
           json_extract(stats, '$.ic') as ic
    FROM hypotheses
    WHERE status = 'backtesting'
    AND json_extract(stats, '$.total_signals') >= 5
    ORDER BY json_extract(stats, '$.total_signals') DESC
""").fetchall()
print(f"  Hypotheses with 5+ signals: {len(rows)}")
for r in rows:
    print(f"    {r['name']}: {r['signals']} sig, edge={r['avg_edge']}, p={r['p_value']}, brier={r['brier']}, ic={r['ic']}")

print()
print('=== BOOK COVERAGE ON SIGNALS ===')
rows = conn.execute("SELECT COUNT(*) as cnt FROM backtest_events").fetchone()
print(f"  Total backtest events: {rows['cnt']}")

rows = conn.execute("SELECT COUNT(DISTINCT bookmaker) as books FROM backtest_events WHERE is_signal = 1").fetchone()
print(f"  Distinct books on signals: {rows['books']}")

rows = conn.execute("SELECT bookmaker, COUNT(*) as cnt FROM backtest_events WHERE is_signal = 1 GROUP BY bookmaker ORDER BY cnt DESC LIMIT 10").fetchall()
for r in rows:
    print(f"    {r['bookmaker']}: {r['cnt']} signals")

print()
print('=== STATUS BREAKDOWN ===')
rows = conn.execute("SELECT status, COUNT(*) as cnt FROM hypotheses GROUP BY status").fetchall()
for r in rows:
    print(f"  {r['status']}: {r['cnt']}")

print()
print('=== STALENESS ===')
rows = conn.execute("""
    SELECT status, COUNT(*) as cnt,
           MIN(updated_at) as oldest_update,
           MAX(updated_at) as newest_update
    FROM hypotheses WHERE status IN ('draft','backtesting') GROUP BY status
""").fetchall()
for r in rows:
    print(f"  {r['status']}: {r['cnt']}, oldest={r['oldest_update']}, newest={r['newest_update']}")

conn.close()
