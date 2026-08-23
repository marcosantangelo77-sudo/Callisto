import sqlite3
conn = sqlite3.connect('data/callisto.db')
conn.row_factory = sqlite3.Row
c = conn.cursor()

print('=== BACKTESTING HYPOTHESES ===')
rows = c.execute("""SELECT id, name, status, sport, market, brier_score, information_coefficient,
    (SELECT COUNT(*) FROM backtest_events WHERE hypothesis_id=h.id) as events,
    (SELECT COUNT(*) FROM backtest_events WHERE hypothesis_id=h.id AND is_signal=1) as signals
    FROM hypotheses h WHERE status='backtesting' ORDER BY events DESC""").fetchall()
for r in rows:
    print(f'{r["id"][:11]} | {r["name"][:45]:45s} | ev={r["events"]:3d} sig={r["signals"]:3d} | brier={r["brier_score"]} IC={r["information_coefficient"]}')

print()
print('=== ZERO-SIGNAL (50+ events) ===')
rows2 = c.execute("""SELECT h.id, h.name, h.status,
    (SELECT COUNT(*) FROM backtest_events WHERE hypothesis_id=h.id) as events,
    (SELECT COUNT(*) FROM backtest_events WHERE hypothesis_id=h.id AND is_signal=1) as signals
    FROM hypotheses h
    HAVING events >= 50 AND signals = 0
    ORDER BY events DESC LIMIT 15""").fetchall()
for r in rows2:
    print(f'{r["id"][:11]} | {r["name"][:50]:50s} | {r["status"]:12s} | ev={r["events"]}')

print()
print('=== ANTI-PREDICTIVE (IC < -0.05) ===')
rows3 = c.execute("""SELECT id, name, status, information_coefficient, brier_score
    FROM hypotheses WHERE information_coefficient < -0.05 AND status != 'rejected'""").fetchall()
for r in rows3:
    print(f'{r["id"][:11]} | {r["name"][:45]:45s} | {r["status"]:12s} | IC={r["information_coefficient"]} brier={r["brier_score"]}')

print()
print('=== STALLED DRAFT (>48h) sample ===')
rows4 = c.execute("""SELECT id, name, sport FROM hypotheses WHERE status='draft'
    AND datetime(created_at) < datetime('now', '-48 hours') LIMIT 10""").fetchall()
for r in rows4:
    print(f'{r["id"][:11]} | {r["name"][:50]:50s} | {r["sport"]}')

print()
print('=== STATS ===')
total = c.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0]
rejected = c.execute("SELECT COUNT(*) FROM hypotheses WHERE status='rejected'").fetchone()[0]
bt = c.execute("SELECT COUNT(*) FROM hypotheses WHERE status='backtesting'").fetchone()[0]
draft = c.execute("SELECT COUNT(*) FROM hypotheses WHERE status='draft'").fetchone()[0]
pt = c.execute("SELECT COUNT(*) FROM hypotheses WHERE status='paper_trading'").fetchone()[0]
print(f'Total={total} Rejected={rejected} Backtesting={bt} Draft={draft} PaperTrading={pt}')

events = c.execute("SELECT COUNT(*) FROM backtest_events").fetchone()[0]
signals = c.execute("SELECT COUNT(*) FROM backtest_events WHERE is_signal=1").fetchone()[0]
print(f'Events={events} Signals={signals} Rate={signals/events*100:.1f}%' if events else 'No events')

conn.close()

