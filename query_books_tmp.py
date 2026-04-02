import sqlite3
conn = sqlite3.connect('data/callisto.db')
cur = conn.cursor()
cur.execute("""
SELECT h.name, 
       AVG(LENGTH(be.books_used) - LENGTH(REPLACE(be.books_used, ',', '')) + 1) as avg_books, 
       COUNT(*) as event_count 
FROM backtest_events be 
JOIN hypotheses h ON h.id = be.hypothesis_id 
WHERE h.status = 'backtesting' AND be.books_used IS NOT NULL 
GROUP BY be.hypothesis_id 
ORDER BY avg_books ASC 
LIMIT 15
""")
rows = cur.fetchall()
for r in rows:
    print(f"{r[0]} | avg_books={r[1]:.1f} | events={r[2]}")
conn.close()
