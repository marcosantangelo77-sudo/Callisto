import sqlite3
conn = sqlite3.connect('data/hypotheses.db')

print('=== STATUS COUNTS ===')
for r in conn.execute('SELECT status, COUNT(*) FROM hypotheses GROUP BY status ORDER BY COUNT(*) DESC'):
    print(f'  {r[0]}: {r[1]}')

print('\n=== BACKTESTING WITH SIGNALS ===')
for r in conn.execute('''SELECT name, total_events, signal_count, win_rate, p_value, edge_avg, evaluate_cycles
    FROM hypotheses WHERE status="backtesting" AND signal_count > 0 ORDER BY signal_count DESC LIMIT 15'''):
    print(f'  {r[0]}: {r[1]}ev, {r[2]}sig, wr={r[3]}, p={r[4]}, edge={r[5]}, eval_cycles={r[6]}')

print('\n=== IDENTICAL EVENT SETS ===')
for r in conn.execute('''SELECT name, total_events, signal_count, edge_avg, unique_games
    FROM hypotheses WHERE name IN ("nhl_ref_crew_penalty_tendency_total", "nhl_goalie_shot_volume_cliff_over")'''):
    print(f'  {r[0]}: events={r[1]}, signals={r[2]}, edge={r[3]}, unique_games={r[4]}')

print('\n=== 0-SIGNAL BACKTESTING ===')
r = conn.execute('SELECT COUNT(*) FROM hypotheses WHERE status="backtesting" AND (signal_count IS NULL OR signal_count = 0)').fetchone()
print(f'  {r[0]} hypotheses with 0 signals')

print('\n=== CLOSEST TO PROMOTION (5+ signals) ===')
for r in conn.execute('''SELECT name, signal_count, p_value, win_rate, total_events, edge_avg
    FROM hypotheses WHERE status="backtesting" AND signal_count >= 5 ORDER BY p_value ASC LIMIT 10'''):
    print(f'  {r[0]}: {r[1]}sig, p={r[2]}, wr={r[3]}, ev={r[4]}, edge={r[5]}')

print('\n=== EVAL CYCLE DISTRIBUTION ===')
for r in conn.execute('SELECT evaluate_cycles, COUNT(*) FROM hypotheses WHERE status="backtesting" GROUP BY evaluate_cycles ORDER BY evaluate_cycles'):
    print(f'  eval_cycles={r[0]}: {r[1]}')

conn.close()
