import sqlite3
conn = sqlite3.connect('memory/callisto.db')
c = conn.cursor()

print('=== HYPOTHESIS STATUS ===')
for row in c.execute('SELECT status, COUNT(*) FROM hypotheses GROUP BY status'):
    print(f'  {row[0]}: {row[1]}')

print('\n=== TOP BACKTESTING HYPOTHESES (by signals) ===')
rows = c.execute('''
    SELECT h.name, h.sport, h.market_type, 
           COUNT(DISTINCT be.id) as events, 
           SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals,
           AVG(CASE WHEN be.signal_generated=1 THEN be.edge ELSE NULL END) as avg_edge,
           AVG(be.edge) as avg_all_edge,
           MIN(be.game_date) as first_date,
           MAX(be.game_date) as last_date
    FROM hypotheses h 
    JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id 
    WHERE h.status='backtesting' 
    GROUP BY h.hypothesis_id 
    ORDER BY signals DESC, events DESC 
    LIMIT 25
''').fetchall()
for r in rows:
    sig_rate = r[4]/r[3]*100 if r[3]>0 else 0
    print(f'  {r[0]} [{r[1]}/{r[2]}]: {r[3]}ev, {r[4]}sig ({sig_rate:.1f}%), avg_sig_edge={r[5]}, avg_edge={r[6]:.4f}, dates={r[7]} to {r[8]}')

print('\n=== PROMOTION CANDIDATES (5+ signals) ===')
rows = c.execute('''
    SELECT h.name, h.sport, h.market_type,
           COUNT(DISTINCT be.id) as events,
           SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals,
           AVG(CASE WHEN be.signal_generated=1 THEN be.edge ELSE NULL END) as avg_edge
    FROM hypotheses h 
    JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id 
    WHERE h.status='backtesting'
    GROUP BY h.hypothesis_id 
    HAVING signals >= 5
    ORDER BY signals DESC
''').fetchall()
if not rows:
    print('  NONE - no hypothesis has 5+ signals')
else:
    for r in rows:
        print(f'  {r[0]}: events={r[3]}, signals={r[4]}, avg_edge={r[5]}')

print('\n=== HYPOTHESES WITH 0 SIGNALS AND 50+ EVENTS (reject candidates) ===')
rows = c.execute('''
    SELECT h.hypothesis_id, h.name, h.sport, h.market_type,
           COUNT(DISTINCT be.id) as events,
           SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals
    FROM hypotheses h 
    JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id 
    WHERE h.status='backtesting'
    GROUP BY h.hypothesis_id 
    HAVING signals = 0 AND events >= 50
    ORDER BY events DESC
''').fetchall()
for r in rows:
    print(f'  {r[0]}: {r[1]} [{r[2]}/{r[3]}]: {r[4]} events, 0 signals')

print('\n=== RECENT REJECTIONS (last 10) ===')
rows = c.execute("""
    SELECT name, sport, notes, updated_at 
    FROM hypotheses 
    WHERE status='rejected' 
    ORDER BY updated_at DESC 
    LIMIT 10
""").fetchall()
for r in rows:
    notes = (r[2] or 'NULL')[:100]
    print(f'  {r[0]} [{r[1]}]: {notes} ({r[3]})')

print('\n=== SIGNAL EVENTS BOOK COVERAGE ===')
rows = c.execute('''
    SELECT book, COUNT(*) as cnt, AVG(edge) as avg_edge
    FROM backtest_events
    WHERE signal_generated = 1
    GROUP BY book
    ORDER BY cnt DESC
    LIMIT 15
''').fetchall()
for r in rows:
    print(f'  {r[0]}: {r[1]} signal events, avg_edge={r[2]:.4f}')

print('\n=== TOTAL SIGNAL STATS ===')
row = c.execute('SELECT COUNT(*), SUM(CASE WHEN signal_generated=1 THEN 1 ELSE 0 END) FROM backtest_events').fetchone()
print(f'  Total events: {row[0]}, Signals: {row[1]}, Rate: {row[1]/row[0]*100:.1f}%' if row[0] else '  No events')

conn.close()
