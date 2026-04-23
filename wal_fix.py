import sqlite3, os
db = os.path.join(os.path.dirname(os.path.abspath(__file__)), "memory", "callisto.db")
print(f"DB: {db}")
conn = sqlite3.connect(db)
print("Connected")
result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
print(f"WAL checkpoint: {result}")
conn.close()
print("Done")
