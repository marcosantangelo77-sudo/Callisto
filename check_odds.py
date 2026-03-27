import sqlite3, json
db = sqlite3.connect('data/odds.db')
tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
print('Tables:', [t[0] for t in tables])
for t in tables:
    cnames = [c[1] for c in db.execute("PRAGMA table_info(%s)" % t[0]).fetchall()]
    print("%s: %s" % (t[0], cnames))
