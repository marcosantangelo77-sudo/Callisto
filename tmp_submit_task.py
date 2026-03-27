"""One-shot: insert Tatum research task into task_queue."""
import sqlite3, sys
from datetime import datetime, timezone

db = sqlite3.connect("memory/callisto.db")
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=30000")

query = (
    "Analyze Jayson Tatum return impact on Celtics teammates for betting edges. "
    "Compare stats WITH vs WITHOUT Tatum this season. Key angles: "
    "(1) Derrick White 3PA and 3P pct shifts with Tatum back, "
    "(2) All Celtics starters shot attempts and distribution changes, "
    "(3) Team offensive and defensive rating and pace differences, "
    "(4) Any teammate whose usage or shot profile changed enough to create a prop edge "
    "books have not adjusted for. Use player_stats and game_contexts data. "
    "Tatum back on semi-limited usage. Want actionable prop or game total edges "
    "on upcoming Celtics games based on with/without splits."
)

cur = db.execute(
    "INSERT INTO task_queue (query, priority, status, created_at) VALUES (?, 1, 'PENDING', ?)",
    (query, datetime.now(timezone.utc).isoformat()),
)
db.commit()
print(f"Task inserted: task_id={cur.lastrowid}")
db.close()
