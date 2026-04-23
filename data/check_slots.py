import sqlite3
conn = sqlite3.connect('memory/callisto.db')
c = conn.cursor()
names = [
    'nhl_sandwich_spot_total_over',
    'nhl_sandwich_total_under',
    'nba_complacency_fade',
    'nba_bubble_team_underdog',
    'nba_dominant_win_letdown',
    'nba_blowout_winner_spread_fade',
]
placeholders = ','.join(['?'] * len(names))
c.execute(f'SELECT name, status, ic_score, rejection_reason FROM hypotheses WHERE name IN ({placeholders}) ORDER BY name', names)
rows = c.fetchall()
if not rows:
    print('No matching hypotheses found for those exact names.')
    print('Searching broader...')
    c.execute("SELECT name, status, ic_score FROM hypotheses WHERE name LIKE '%sandwich%' OR name LIKE '%complacency%' OR name LIKE '%bubble_team%' OR name LIKE '%dominant_win%' OR name LIKE '%blowout_winner_spread%'")
    for row in c.fetchall():
        print(f'  {row[0]} | status={row[1]} | IC={row[2]}')
else:
    for row in rows:
        print(f'{row[0]} | status={row[1]} | IC={row[2]} | reason={(row[3] or "")[:120]}')

print('\n--- Status Distribution ---')
c.execute('SELECT status, COUNT(*) FROM hypotheses GROUP BY status ORDER BY COUNT(*) DESC')
for row in c.fetchall():
    print(f'  {row[0]}: {row[1]}')

print('\n--- Recent Rejections (last 10) ---')
c.execute("SELECT name, ic_score, rejection_reason FROM hypotheses WHERE status='rejected' ORDER BY rowid DESC LIMIT 10")
for row in c.fetchall():
    print(f'  {row[0]} | IC={row[1]} | {(row[2] or "")[:100]}')

conn.close()
