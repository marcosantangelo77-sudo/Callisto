import sqlite3, json, os
db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'callisto.db')
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()
cur.execute("""SELECT h.id, h.name, h.sport, h.market_type, h.status,
  (SELECT COUNT(*) FROM backtest_events be WHERE be.hypothesis_id = h.id) as events,
  (SELECT COUNT(*) FROM backtest_events be WHERE be.hypothesis_id = h.id AND be.edge_pct > 0) as signals
FROM hypotheses h WHERE h.status = 'backtesting' ORDER BY events DESC LIMIT 25""")
rows = [dict(r) for r in cur.fetchall()]
print(json.dumps(rows, indent=2))
conn.close()
