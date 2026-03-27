import sqlite3, json, sys

for db_name in ['data/odds.db', 'data/callisto.db', 'data/research.db']:
    try:
        conn = sqlite3.connect(db_name)
        cur = conn.cursor()
        cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
        tables = [r[0] for r in cur.fetchall()]
        print(f"=== {db_name}: {tables} ===")

        for t in tables:
            cur.execute(f'SELECT * FROM [{t}] LIMIT 1')
            cols = [d[0] for d in cur.description]
            for c in cols:
                if any(k in c.lower() for k in ['team','name','event','game','home','away']):
                    try:
                        cur.execute(f"SELECT * FROM [{t}] WHERE [{c}] LIKE '%Mariner%' OR [{c}] LIKE '%Seattle%' OR [{c}] LIKE '%Guardian%' OR [{c}] LIKE '%Cleveland%' ORDER BY rowid DESC LIMIT 5")
                        rows = cur.fetchall()
                        if rows:
                            print(f"Table {t}, col {c}, Cols: {cols}")
                            for r in rows:
                                print(r)
                    except:
                        pass
        conn.close()
    except Exception as e:
        print(f"{db_name}: {e}")
sys.stdout.flush()
