import sqlite3, json, sys

try:
    conn = sqlite3.connect(r'C:\Users\marco\OneDrive\Desktop\Callisto\data\callisto.db')
    conn.row_factory = sqlite3.Row

    # List tables
    cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    with open(r'C:\Users\marco\OneDrive\Desktop\Callisto\tmp_okc_result.txt', 'w') as f:
        f.write(f"Tables: {tables}\n\n")

        # Search for Thunder/Celtics in odds_snapshots
        if 'odds_snapshots' in tables:
            cur2 = conn.execute("SELECT COUNT(*) FROM odds_snapshots")
            count = cur2.fetchone()[0]
            f.write(f"Total odds_snapshots: {count}\n")

            cur3 = conn.execute("SELECT DISTINCT home_team, away_team FROM odds_snapshots LIMIT 10")
            f.write(f"Sample teams: {[dict(r) for r in cur3.fetchall()]}\n\n")

            # Search broadly
            cur4 = conn.execute("SELECT * FROM odds_snapshots WHERE home_team LIKE '%Thunder%' OR away_team LIKE '%Thunder%' ORDER BY snapshot_time DESC LIMIT 3")
            rows = cur4.fetchall()
            f.write(f"Thunder rows: {len(rows)}\n")
            for r in rows:
                f.write(json.dumps(dict(r), default=str) + "\n")

        # Check edges
        if 'edges' in tables:
            cur5 = conn.execute("SELECT COUNT(*) FROM edges")
            f.write(f"\nTotal edges: {cur5.fetchone()[0]}\n")
            cur6 = conn.execute("SELECT * FROM edges ORDER BY rowid DESC LIMIT 3")
            for r in cur6.fetchall():
                f.write(json.dumps(dict(r), default=str) + "\n")

    conn.close()

except Exception as e:
    with open(r'C:\Users\marco\OneDrive\Desktop\Callisto\tmp_okc_result.txt', 'w') as f:
        f.write(f"ERROR: {e}\n")
        import traceback
        traceback.print_exc(file=f)
