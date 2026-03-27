import sqlite3

db = sqlite3.connect('data/callisto.db')
c = db.cursor()

print('=== HYPOTHESIS STATUS ===')
for row in c.execute('SELECT status, COUNT(*) FROM hypotheses GROUP BY status'):
    print(f'  {row[0]}: {row[1]}')

print('\n=== BACKTEST EVENTS ===')
row = c.execute('SELECT COUNT(*), SUM(signal_generated) FROM backtest_events').fetchone()
print(f'  Total events: {row[0]}, Signals: {row[1]}')

# Unresolved
try:
    row = c.execute("SELECT COUNT(*) FROM backtest_events WHERE resolved = 0 OR resolved IS NULL").fetchone()
    print(f'  Unresolved: {row[0]}')
except:
    print('  (no resolved column)')

print('\n=== TOP BACKTESTING HYPOTHESES ===')
for row in c.execute('''
    SELECT h.name, h.edge_threshold, COUNT(be.id) as events,
           SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals
    FROM hypotheses h
    LEFT JOIN backtest_events be ON h.id = be.hypothesis_id
    WHERE h.status='backtesting'
    GROUP BY h.id ORDER BY events DESC LIMIT 15
'''):
    print(f'  {row[0]}: thr={row[1]}, events={row[2]}, signals={row[3]}')

print('\n=== EDGE THRESHOLDS ===')
for row in c.execute('SELECT edge_threshold, COUNT(*) FROM hypotheses WHERE status="backtesting" GROUP BY edge_threshold ORDER BY edge_threshold'):
    print(f'  {row[0]}: {row[1]} hypotheses')

print('\n=== RECENTLY REJECTED ===')
for row in c.execute('SELECT name, edge_threshold, rejection_reason FROM hypotheses WHERE status="rejected" ORDER BY updated_at DESC LIMIT 10'):
    r = (row[2] or 'none')[:120]
    print(f'  {row[0]}: thr={row[1]}, reason={r}')

print('\n=== SIGNALS TABLE ===')
try:
    row = c.execute('SELECT COUNT(*) FROM signals').fetchone()
    print(f'  Total signals: {row[0]}')
    for row in c.execute('SELECT hypothesis_id, sport, edge, confidence FROM signals ORDER BY created_at DESC LIMIT 5'):
        print(f'  h_id={row[0]}, sport={row[1]}, edge={row[2]}, conf={row[3]}')
except Exception as e:
    print(f'  Error: {e}')

print('\n=== BACKTEST RUNS ===')
try:
    row = c.execute('SELECT COUNT(*) FROM backtest_runs').fetchone()
    print(f'  Total runs: {row[0]}')
    for row in c.execute('SELECT hypothesis_id, events_found, signals_generated, status FROM backtest_runs ORDER BY created_at DESC LIMIT 5'):
        print(f'  h_id={row[0]}, events={row[1]}, signals={row[2]}, status={row[3]}')
except Exception as e:
    print(f'  Error: {e}')

db.close()
