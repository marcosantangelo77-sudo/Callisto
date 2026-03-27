import sqlite3, json

conn = sqlite3.connect('memory/callisto.db')
cur = conn.cursor()

# Get the most recent NCAAB snapshot
cur.execute("SELECT snapshot_json FROM odds_snapshots WHERE sport='basketball_ncaab' ORDER BY timestamp DESC LIMIT 1")
row = cur.fetchone()
if row:
    data = json.loads(row[0])
    for game in data.get('games', []):
        if 'arizona' in game.get('home_team', '').lower() or 'arkansas' in game.get('away_team', '').lower():
            print(json.dumps(game, indent=2))

# Also check ev_opportunities
cur.execute("SELECT * FROM ev_opportunities ORDER BY rowid DESC LIMIT 50")
cols = [d[0] for d in cur.description]
for r in cur.fetchall():
    s = str(r).lower()
    if 'arizona' in s or 'arkansas' in s:
        d = dict(zip(cols, r))
        print('\nEV OPP:', json.dumps(d, indent=2, default=str))

# Check signals
cur.execute("SELECT * FROM signals ORDER BY rowid DESC LIMIT 50")
cols = [d[0] for d in cur.description]
for r in cur.fetchall():
    s = str(r).lower()
    if 'arizona' in s or 'arkansas' in s:
        d = dict(zip(cols, r))
        print('\nSIGNAL:', json.dumps(d, indent=2, default=str))

conn.close()
