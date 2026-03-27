import sqlite3, json

# Check memory/callisto.db
conn = sqlite3.connect('memory/callisto.db')
conn.row_factory = sqlite3.Row

cur = conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
tables = [r[0] for r in cur.fetchall()]
print("callisto.db tables:", tables[:30])

# Check for edges/odds related to Mariners
for t in tables:
    try:
        cur = conn.execute(f"SELECT * FROM {t} LIMIT 1")
        row = cur.fetchone()
        if row:
            cols = [d[0] for d in cur.description]
            print(f"\n{t} columns: {cols}")
            s = str(dict(row))
            if len(s) < 500:
                print(f"  sample: {s}")
    except Exception as e:
        print(f"  {t} error: {e}")

# Search for Mariners in edges/signals
for t in ['edges', 'signals', 'hypotheses', 'odds_snapshots']:
    if t in tables:
        try:
            rows = conn.execute(f"SELECT * FROM {t} WHERE CAST(* AS TEXT) LIKE '%Mariners%'").fetchall()
        except:
            pass
        # Try column by column
        cur2 = conn.execute(f"PRAGMA table_info({t})")
        cols = [c[1] for c in cur2.fetchall()]
        for col in cols:
            try:
                rows = conn.execute(f"SELECT * FROM {t} WHERE {col} LIKE '%Mariners%' OR {col} LIKE '%Seattle%' OR {col} LIKE '%Guardians%' OR {col} LIKE '%Cleveland%' LIMIT 5").fetchall()
                if rows:
                    print(f"\n=== {t}.{col} matches ===")
                    for r in rows:
                        print(dict(r))
            except:
                pass

conn.close()

# Also check data/odds.db
try:
    conn2 = sqlite3.connect('data/odds.db')
    conn2.row_factory = sqlite3.Row
    cur = conn2.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables2 = [r[0] for r in cur.fetchall()]
    print("\n\nodds.db tables:", tables2[:30])

    for t in tables2:
        cur2 = conn2.execute(f"PRAGMA table_info({t})")
        cols = [c[1] for c in cur2.fetchall()]
        print(f"  {t}: {cols}")
        for col in cols:
            try:
                rows = conn2.execute(f"SELECT * FROM {t} WHERE {col} LIKE '%Mariners%' OR {col} LIKE '%Seattle%' LIMIT 3").fetchall()
                if rows:
                    print(f"\n=== odds.db {t}.{col} matches ===")
                    for r in rows:
                        print(dict(r))
            except:
                pass
    conn2.close()
except Exception as e:
    print(f"odds.db error: {e}")
