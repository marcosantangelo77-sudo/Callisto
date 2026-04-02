import sqlite3
from collections import defaultdict

conn = sqlite3.connect('data/callisto.db')
c = conn.cursor()

# 1. Book coverage
c.execute('SELECT AVG(books_used), MIN(books_used), MAX(books_used), COUNT(*) FROM backtest_events WHERE books_used IS NOT NULL')
row = c.fetchone()
print(f'Books per event: avg={row[0]:.1f}, min={row[1]}, max={row[2]}, count={row[3]}')
c.execute('SELECT books_used, COUNT(*) FROM backtest_events WHERE books_used IS NOT NULL GROUP BY books_used ORDER BY books_used')
for r in c.fetchall():
    print(f'  {r[0]} books: {r[1]} events')

print()

# 2. Identical event sets
c.execute('''
    SELECT h.name, h.sport, h.market_type, COUNT(DISTINCT e.game_id) as unique_games,
           COUNT(*) as total_events, SUM(CASE WHEN e.is_signal=1 THEN 1 ELSE 0 END) as signals,
           GROUP_CONCAT(DISTINCT e.game_id) as game_ids
    FROM hypotheses h
    JOIN backtest_events e ON h.id = e.hypothesis_id
    WHERE h.status = 'backtesting'
    GROUP BY h.id
    ORDER BY unique_games DESC
''')
rows = c.fetchall()
game_sets = defaultdict(list)
for r in rows:
    key = r[6]
    game_sets[key].append((r[0], r[1], r[2], r[3], r[4], r[5]))

print('=== IDENTICAL EVENT SETS ===')
dupes_found = False
for gids, hyps in game_sets.items():
    if len(hyps) > 1:
        dupes_found = True
        print(f'\nSHARED SET ({hyps[0][3]} games, {len(hyps)} hypotheses):')
        for h in hyps:
            print(f'  {h[0]} [{h[1]}/{h[2]}]: {h[4]} events, {h[5]} signals')
if not dupes_found:
    print('  No identical event sets found')

print()

# 3. Promotion gate values
c.execute('''
    SELECT h.name, h.sport, h.win_rate, h.p_value, h.brier_score, h.ic_score,
           COUNT(DISTINCT e.game_id) as games, SUM(CASE WHEN e.is_signal=1 THEN 1 ELSE 0 END) as signals,
           AVG(CASE WHEN e.is_signal=1 THEN e.edge_pct ELSE NULL END) as avg_edge
    FROM hypotheses h
    LEFT JOIN backtest_events e ON h.id = e.hypothesis_id
    WHERE h.status = 'backtesting'
    GROUP BY h.id
    ORDER BY signals DESC
    LIMIT 15
''')
print('=== TOP BACKTESTING - GATE VALUES ===')
for r in c.fetchall():
    wr = f'{r[2]:.2f}' if r[2] else 'None'
    pv = f'{r[3]:.4f}' if r[3] else 'None'
    br = f'{r[4]:.4f}' if r[4] else 'None'
    ic = f'{r[5]:.4f}' if r[5] else 'None'
    ae = f'{r[8]:.4f}' if r[8] else 'None'
    print(f'  {r[0]:<55} {r[1]:<12} WR={wr} p={pv} brier={br} IC={ic} games={r[6]} sigs={r[7]} edge={ae}')

print()

# 4. Recent rejection reasons
c.execute('''
    SELECT rejection_reason, COUNT(*) FROM hypotheses
    WHERE status = 'rejected' AND created_at > datetime('now', '-12 hours')
    GROUP BY rejection_reason ORDER BY COUNT(*) DESC LIMIT 10
''')
print('=== RECENT REJECTION REASONS ===')
for r in c.fetchall():
    reason = (r[0] or 'unknown')[:80]
    print(f'  {reason}: {r[1]}')

print()

# 5. Signal rate by sport
c.execute('''
    SELECT h.sport, COUNT(*) as events, SUM(CASE WHEN e.is_signal=1 THEN 1 ELSE 0 END) as signals,
           AVG(CASE WHEN e.is_signal=1 THEN e.edge_pct ELSE NULL END) as avg_edge
    FROM backtest_events e
    JOIN hypotheses h ON h.id = e.hypothesis_id
    WHERE h.status = 'backtesting'
    GROUP BY h.sport
''')
print('=== SIGNAL RATE BY SPORT ===')
for r in c.fetchall():
    sig_rate = r[2]/r[1]*100 if r[1] else 0
    ae = f'{r[3]:.4f}' if r[3] else 'N/A'
    print(f'  {r[0]:<25} {r[1]:>6} events, {r[2]:>4} signals ({sig_rate:.1f}%), avg_edge={ae}')

# 6. Check how many hypotheses have NULL stats
c.execute("SELECT COUNT(*) FROM hypotheses WHERE status='backtesting' AND (p_value IS NULL OR win_rate IS NULL)")
null_stats = c.fetchone()[0]
c.execute("SELECT COUNT(*) FROM hypotheses WHERE status='backtesting'")
total_bt = c.fetchone()[0]
print(f'\nHypotheses with NULL stats: {null_stats}/{total_bt}')

conn.close()
