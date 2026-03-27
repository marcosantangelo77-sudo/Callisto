import sqlite3, json

conn = sqlite3.connect('data/callisto.db')
conn.row_factory = sqlite3.Row

# Check for OKC/Celtics odds
cur = conn.execute("""
    SELECT * FROM odds_snapshots
    WHERE home_team LIKE '%Thunder%' OR away_team LIKE '%Thunder%'
       OR home_team LIKE '%Celtics%' OR away_team LIKE '%Celtics%'
    ORDER BY snapshot_time DESC LIMIT 5
""")
rows = cur.fetchall()
if rows:
    for r in rows:
        print(json.dumps(dict(r), indent=2, default=str))
else:
    print("No odds snapshots for Thunder/Celtics")

# Check edges table
try:
    cur2 = conn.execute("""
        SELECT * FROM edges
        WHERE team LIKE '%Thunder%' OR team LIKE '%Oklahoma%'
        ORDER BY detected_at DESC LIMIT 5
    """)
    rows2 = cur2.fetchall()
    if rows2:
        print("\n--- EDGES ---")
        for r in rows2:
            print(json.dumps(dict(r), indent=2, default=str))
    else:
        print("\nNo edges for Thunder/OKC")
except:
    print("\nNo edges table or different schema")

# Check what DK/Fanatics lines look like for this game
try:
    cur3 = conn.execute("""
        SELECT bookmaker, market, outcome_name, price
        FROM odds_snapshots
        WHERE (home_team LIKE '%Thunder%' OR away_team LIKE '%Thunder%')
          AND bookmaker IN ('draftkings', 'fanduel', 'fanatics', 'pointsbet', 'pinnacle')
        ORDER BY snapshot_time DESC LIMIT 20
    """)
    rows3 = cur3.fetchall()
    if rows3:
        print("\n--- BOOK COMPARISON ---")
        for r in rows3:
            print(dict(r))
except Exception as e:
    print(f"\nBook comparison failed: {e}")

conn.close()
