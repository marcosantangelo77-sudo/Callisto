import sqlite3, json

conn = sqlite3.connect("memory/callisto.db")
c = conn.cursor()

# Top hypothesis - the one with most signals
c.execute("""SELECT h.hypothesis_id, h.name, h.status, h.edge_threshold, h.model_config,
             COUNT(*) as total,
             SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as sigs
             FROM backtest_events be
             JOIN hypotheses h ON be.hypothesis_id=h.hypothesis_id
             GROUP BY h.hypothesis_id
             ORDER BY sigs DESC LIMIT 5""")
print("=== Top hypotheses by signals ===")
for r in c.fetchall():
    hid, name, status, thresh, cfg_str, total, sigs = r
    cfg = json.loads(cfg_str) if cfg_str else {}
    print(f"\n{name}")
    print(f"  ID: {hid}, Status: {status}, Threshold: {thresh}")
    print(f"  eval_cycles: {cfg.get('evaluate_cycles', 0)}")
    print(f"  Events: {total}, Signals: {sigs}")

    # Signal details
    c.execute("SELECT edge, book, actual_result, game_date FROM backtest_events WHERE hypothesis_id=? AND signal_generated=1 ORDER BY edge DESC", (hid,))
    w = l = u = 0
    for s in c.fetchall():
        outcome = s[2] or "unresolved"
        print(f"  Signal: edge={s[0]:.4f}, book={s[1]}, outcome={outcome}, date={s[3]}")
        if s[2] == "win": w += 1
        elif s[2] == "loss": l += 1
        else: u += 1
    print(f"  Record: {w}W-{l}L-{u}U")

# Promotion readiness simulation
print("\n=== Promotion Gate Analysis ===")
c.execute("""SELECT h.hypothesis_id, h.name,
             SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as sigs,
             SUM(CASE WHEN be.signal_generated=1 AND be.actual_result='win' THEN 1 ELSE 0 END) as wins,
             SUM(CASE WHEN be.signal_generated=1 AND be.actual_result='loss' THEN 1 ELSE 0 END) as losses
             FROM backtest_events be
             JOIN hypotheses h ON be.hypothesis_id=h.hypothesis_id
             WHERE h.status = 'backtesting'
             GROUP BY h.hypothesis_id HAVING sigs >= 5
             ORDER BY sigs DESC""")
for r in c.fetchall():
    hid, name, sigs, wins, losses = r
    resolved = wins + losses
    if resolved > 0:
        hit_rate = wins / resolved
        print(f"  {name}: {sigs} signals, {wins}W-{losses}L ({hit_rate:.1%})")
    else:
        print(f"  {name}: {sigs} signals, {wins}W-{losses}L (unresolved)")

# Status distribution
c.execute("SELECT status, COUNT(*) FROM hypotheses GROUP BY status ORDER BY COUNT(*) DESC")
print("\n=== Status distribution ===")
for r in c.fetchall():
    print(f"  {r[0]}: {r[1]}")

# Average books per event
c.execute("SELECT AVG(json_array_length(model_factors)) FROM backtest_events WHERE model_factors IS NOT NULL AND model_factors != '' AND model_factors != 'null' LIMIT 1")
# Fallback - count distinct books per event
c.execute("""SELECT event_id, COUNT(DISTINCT book) as book_count
             FROM backtest_events GROUP BY event_id""")
book_counts = [r[1] for r in c.fetchall()]
if book_counts:
    avg_books = sum(book_counts) / len(book_counts)
    print(f"\nAvg books per event: {avg_books:.1f}")
    print(f"Events with 1 book: {sum(1 for b in book_counts if b == 1)}")
    print(f"Events with 2+ books: {sum(1 for b in book_counts if b >= 2)}")
    print(f"Events with 3+ books: {sum(1 for b in book_counts if b >= 3)}")

# Rejection candidates
c.execute("""SELECT h.hypothesis_id, h.name, COUNT(*) as total,
             SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as sigs,
             h.status, h.model_config
             FROM backtest_events be
             JOIN hypotheses h ON be.hypothesis_id=h.hypothesis_id
             WHERE h.status = 'backtesting'
             GROUP BY h.hypothesis_id HAVING total >= 50 AND sigs = 0
             ORDER BY total DESC""")
print("\n=== Rejection candidates (50+ events, 0 signals, backtesting) ===")
reject_ids = []
for r in c.fetchall():
    cfg = json.loads(r[5]) if r[5] else {}
    cycles = cfg.get("evaluate_cycles", 0)
    print(f"  {r[1]}: {r[2]} events, {r[3]} signals, {cycles} cycles")
    reject_ids.append(r[0])
print(f"\nReject IDs: {reject_ids}")

# Edge distribution
c.execute("SELECT edge FROM backtest_events WHERE edge IS NOT NULL ORDER BY edge DESC LIMIT 50")
edges = [r[0] for r in c.fetchall()]
if edges:
    print(f"\nEdge distribution (top 10): {[round(e,4) for e in edges[:10]]}")
    print(f"Median edge: {sorted(edges)[len(edges)//2]:.4f}")
    above_1pct = sum(1 for e in edges if e >= 0.01)
    above_2pct = sum(1 for e in edges if e >= 0.02)
    print(f"Edges >= 1%: {above_1pct}/{len(edges)}")
    print(f"Edges >= 2%: {above_2pct}/{len(edges)}")

# Check evaluate_significance - what does it actually compute p-value on?
c.execute("SELECT hypothesis_id, p_value, hit_rate, avg_edge, total_n, signals_n, is_significant FROM hypothesis_stats ORDER BY computed_at DESC LIMIT 10")
print("\n=== Latest hypothesis_stats ===")
for r in c.fetchall():
    print(f"  {r[0][:12]}... p={r[1]:.4f}, hit={r[2]:.4f}, edge={r[3]:.4f}, n={r[4]}, sigs={r[5]}, sig={r[6]}")

conn.close()
