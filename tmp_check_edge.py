import sqlite3, json, os

db = 'data/callisto.db'
if not os.path.exists(db):
    print('DB missing')
    exit()

conn = sqlite3.connect(db)
conn.row_factory = sqlite3.Row

# MLB odds
try:
    rows = conn.execute("SELECT DISTINCT home_team, away_team, snapshot_time FROM odds_snapshots WHERE sport_key LIKE '%mlb%' ORDER BY snapshot_time DESC LIMIT 5").fetchall()
    if rows:
        for r in rows:
            print(dict(r))
    else:
        print('No MLB odds snapshots found')
except Exception as e:
    print(f'odds error: {e}')

# Scraper learning
try:
    rows = conn.execute("SELECT key, value FROM learnings WHERE key LIKE '%scraper%'").fetchall()
    for r in rows:
        print(f'SCRAPER: {r["value"][:300]}')
except Exception as e:
    print(f'learning error: {e}')

# Check edge log for this game
try:
    rows = conn.execute("SELECT * FROM edge_log WHERE home_team LIKE '%Mariners%' OR away_team LIKE '%Guardians%' ORDER BY timestamp DESC LIMIT 5").fetchall()
    if rows:
        for r in rows:
            print(f'EDGE: {dict(r)}')
    else:
        print('No edge_log entries for this game')
except Exception as e:
    print(f'edge_log error: {e}')
