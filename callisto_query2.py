import sqlite3, os
os.environ['PYTHONIOENCODING'] = 'utf-8'
conn = sqlite3.connect('memory/callisto.db')
c = conn.cursor()

candidates = ['mlb_day_after_extra_innings_f5_under', 'nhl_home_blowout_loss_coaching_bounce', 
              'nhl_sandwich_backup_goalie_under', 'nhl_talent_gap_coast_dog_spread',
              'nhl_weak_team_sandwich_defensive_collapse_over']

for name in candidates:
    row = c.execute('''
        SELECT hypothesis_id, name, sport, market_type, edge_threshold, status,
               min_sample_size, significance_level, notes
        FROM hypotheses WHERE name = ?
    ''', (name,)).fetchone()
    if row:
        hid = row[0]
        notes_clean = (row[8] or "").encode('ascii', 'replace').decode('ascii')[:200]
        print(f'\n{row[1]} [{row[2]}/{row[3]}]')
        print(f'  edge_threshold={row[4]}, min_sample={row[6]}, sig_level={row[7]}')
        print(f'  notes: {notes_clean}')
        
        sigs = c.execute('''
            SELECT actual_result, edge, ev_pct, book, game_date
            FROM backtest_events 
            WHERE hypothesis_id = ? AND signal_generated = 1
            ORDER BY game_date DESC
        ''', (hid,)).fetchall()
        
        wins = sum(1 for s in sigs if s[0] == 'win')
        losses = sum(1 for s in sigs if s[0] == 'loss')
        pushes = sum(1 for s in sigs if s[0] == 'push')
        pending = sum(1 for s in sigs if s[0] is None or s[0] not in ('win','loss','push'))
        print(f'  Signals: {len(sigs)} total, {wins}W-{losses}L-{pushes}P, {pending} pending')
        for s in sigs[:8]:
            edge_str = f'{s[1]:.4f}' if s[1] else 'None'
            print(f'    {s[4]} @ {s[3]}: result={s[0]}, edge={edge_str}')

conn.close()
