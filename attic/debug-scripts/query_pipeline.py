import sqlite3

DB = 'memory/callisto.db'
conn = sqlite3.connect(DB)
conn.row_factory = sqlite3.Row
out = open('pipeline_report.txt', 'w')

def p(s=''):
    out.write(s + '\n')

# Status counts
p('=== STATUS COUNTS ===')
for row in conn.execute('SELECT status, COUNT(*) as cnt FROM hypotheses GROUP BY status ORDER BY cnt DESC'):
    p(f'  {row["status"]}: {row["cnt"]}')

# Hypothesis stats columns
p('\n=== HYPOTHESIS_STATS COLUMNS ===')
try:
    cols = conn.execute("PRAGMA table_info(hypothesis_stats)").fetchall()
    p(f'  {[c["name"] for c in cols]}')
except:
    p('  No hypothesis_stats table')

# Top backtesting hypotheses with full stats
p('\n=== BACKTESTING HYPOTHESES (by signals) ===')
rows = conn.execute('''
    SELECT h.hypothesis_id, h.name, h.sport, h.market_type, h.status,
           COUNT(DISTINCT be.id) as events,
           SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals,
           AVG(CASE WHEN be.signal_generated=1 THEN be.edge ELSE NULL END) as avg_edge,
           h.edge_threshold, h.significance_level
    FROM hypotheses h
    LEFT JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id
    WHERE h.status = 'backtesting'
    GROUP BY h.hypothesis_id
    ORDER BY signals DESC
''').fetchall()
for r in rows:
    p(f'  {r["name"][:55]} [{r["sport"]}/{r["market_type"]}]: {r["events"]}ev, {r["signals"]}sig, edge={r["avg_edge"]}, thresh={r["edge_threshold"]}')

# Stats from hypothesis_stats table
p('\n=== HYPOTHESIS_STATS FOR BACKTESTING ===')
try:
    rows_s = conn.execute('''
        SELECT hs.*, h.name
        FROM hypothesis_stats hs
        JOIN hypotheses h ON h.hypothesis_id = hs.hypothesis_id
        WHERE h.status = 'backtesting'
        ORDER BY hs.total_signals DESC
        LIMIT 15
    ''').fetchall()
    for r in rows_s:
        d = dict(r)
        p(f'  {d.get("name","?")[:45]}: signals={d.get("total_signals")}, events={d.get("total_events")}, wr={d.get("win_rate")}, p={d.get("p_value")}, brier={d.get("brier_score")}, ic={d.get("information_coefficient")}')
except Exception as e:
    p(f'  Error: {e}')

# Promotion candidates
p('\n=== PROMOTION CANDIDATES (20+ events, 5+ signals) ===')
rows2 = conn.execute('''
    SELECT h.hypothesis_id, h.name, h.sport,
           COUNT(DISTINCT be.id) as events,
           SUM(CASE WHEN be.signal_generated=1 THEN 1 ELSE 0 END) as signals
    FROM hypotheses h
    LEFT JOIN backtest_events be ON h.hypothesis_id = be.hypothesis_id
    WHERE h.status = 'backtesting'
    GROUP BY h.hypothesis_id
    HAVING events >= 20 AND signals >= 5
    ORDER BY signals DESC
''').fetchall()
p(f'  Count: {len(rows2)}')
for r in rows2:
    p(f'    {r["name"]}: {r["events"]}ev, {r["signals"]}sig')

# Sample signal events to check book coverage
p('\n=== SIGNAL EVENT SAMPLES ===')
rows3 = conn.execute('''
    SELECT be.hypothesis_id, h.name as hyp_name, be.edge, be.book, be.book_implied_prob, be.model_fair_prob,
           be.game_date, be.event_id
    FROM backtest_events be
    JOIN hypotheses h ON h.hypothesis_id = be.hypothesis_id
    WHERE h.status = 'backtesting' AND be.signal_generated = 1
    ORDER BY be.created_at DESC
    LIMIT 15
''').fetchall()
for r in rows3:
    p(f'  {r["hyp_name"][:30]}: edge={r["edge"]}, book={r["book"]}, book_prob={r["book_implied_prob"]}, fair={r["model_fair_prob"]}, date={r["game_date"]}')

# Recent rejections
p('\n=== RECENT REJECTIONS (last 20) ===')
# Check what rejection_reason column name is
h_cols = conn.execute("PRAGMA table_info(hypotheses)").fetchall()
h_col_names = [c["name"] for c in h_cols]
p(f'  Hypothesis columns: {h_col_names}')

if 'rejection_reason' in h_col_names:
    rej_col = 'rejection_reason'
elif 'notes' in h_col_names:
    rej_col = 'notes'
else:
    rej_col = None

if rej_col:
    rows4 = conn.execute(f'''
        SELECT name, sport, {rej_col} as reason, updated_at
        FROM hypotheses
        WHERE status = 'rejected'
        ORDER BY updated_at DESC
        LIMIT 20
    ''').fetchall()
    for r in rows4:
        reason = (r["reason"] or "NULL")[:80]
        p(f'  {r["name"][:45]} | reason={reason}')

# Event set overlap
p('\n=== EVENT SET OVERLAP (backtesting hyps, >5 shared) ===')
try:
    rows5 = conn.execute('''
        SELECT h1.name as h1_name, h2.name as h2_name,
               COUNT(DISTINCT be1.event_id) as shared
        FROM backtest_events be1
        JOIN backtest_events be2 ON be1.event_id = be2.event_id AND be1.hypothesis_id < be2.hypothesis_id
        JOIN hypotheses h1 ON h1.hypothesis_id = be1.hypothesis_id
        JOIN hypotheses h2 ON h2.hypothesis_id = be2.hypothesis_id
        WHERE h1.status = 'backtesting' AND h2.status = 'backtesting'
        GROUP BY be1.hypothesis_id, be2.hypothesis_id
        HAVING shared > 5
        ORDER BY shared DESC
        LIMIT 15
    ''').fetchall()
    if rows5:
        for r in rows5:
            p(f'  {r["h1_name"][:35]} <-> {r["h2_name"][:35]}: {r["shared"]} shared')
    else:
        p('  No significant overlap')
except Exception as e:
    p(f'  Error (may be slow): {e}')

# Paper trading / promoted / live
p('\n=== PAPER_TRADING / PROMOTED / LIVE ===')
rows7 = conn.execute('''
    SELECT hypothesis_id, name, status, sport
    FROM hypotheses
    WHERE status IN ('paper_trading', 'promoted', 'live')
''').fetchall()
p(f'  Count: {len(rows7)}')
for r in rows7:
    p(f'  {r["name"]} [{r["status"]}] ({r["sport"]})')

# Retired - sample
p('\n=== RETIRED (last 10) ===')
rows8 = conn.execute(f'''
    SELECT name, sport, {rej_col} as reason, updated_at
    FROM hypotheses
    WHERE status = 'retired'
    ORDER BY updated_at DESC
    LIMIT 10
''').fetchall()
for r in rows8:
    reason = (r["reason"] or "NULL")[:60]
    p(f'  {r["name"][:45]} | {reason}')

# Check edge distribution on signals
p('\n=== EDGE DISTRIBUTION ON SIGNALS (backtesting hyps) ===')
rows9 = conn.execute('''
    SELECT
        COUNT(*) as total,
        AVG(be.edge) as avg_edge,
        MIN(be.edge) as min_edge,
        MAX(be.edge) as max_edge,
        SUM(CASE WHEN be.edge > 0.05 THEN 1 ELSE 0 END) as above_5pct,
        SUM(CASE WHEN be.edge > 0.02 THEN 1 ELSE 0 END) as above_2pct,
        SUM(CASE WHEN be.edge > 0.01 THEN 1 ELSE 0 END) as above_1pct
    FROM backtest_events be
    JOIN hypotheses h ON h.hypothesis_id = be.hypothesis_id
    WHERE h.status = 'backtesting' AND be.signal_generated = 1
''').fetchall()
for r in rows9:
    p(f'  Total signals: {r["total"]}, avg_edge: {r["avg_edge"]}, min: {r["min_edge"]}, max: {r["max_edge"]}')
    p(f'  >5%: {r["above_5pct"]}, >2%: {r["above_2pct"]}, >1%: {r["above_1pct"]}')

# Check how model_fair_prob is derived - look at devig implementation
p('\n=== UNIQUE BOOKS IN SIGNAL EVENTS ===')
rows10 = conn.execute('''
    SELECT be.book, COUNT(*) as cnt
    FROM backtest_events be
    JOIN hypotheses h ON h.hypothesis_id = be.hypothesis_id
    WHERE h.status = 'backtesting' AND be.signal_generated = 1
    GROUP BY be.book
    ORDER BY cnt DESC
''').fetchall()
for r in rows10:
    p(f'  {r["book"]}: {r["cnt"]} signals')

out.close()
conn.close()
print("Done")
