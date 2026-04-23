import sqlite3, json, sys

db_path = r'C:\Users\marco\OneDrive\Desktop\Callisto\data\callisto.db'
conn = sqlite3.connect(db_path)
conn.row_factory = sqlite3.Row
cur = conn.cursor()

names = [
    'nba_bubble_desperation_spread_value',
    'nba_bubble_underdog_motivation_spread',
    'nba_blowout_fav_spread_regression',
    'nba_dominant_win_hangover_spread',
    'nba_elite_sandwich_letdown_ats',
    'nba_elite_sandwich_coasting_spread',
]

placeholders = ','.join(['?'] * len(names))
cur.execute(f"SELECT name, sport, market, game_filters, context_filter, cf_set, status FROM hypotheses WHERE name IN ({placeholders})", names)
rows = cur.fetchall()

for r in rows:
    print(f"NAME: {r['name']}")
    print(f"SPORT: {r['sport']}")
    print(f"MARKET: {r['market']}")
    print(f"GAME_FILTERS: {r['game_filters']}")
    print(f"CONTEXT_FILTER: {r['context_filter']}")
    print(f"CF_SET: {r['cf_set']}")
    print(f"STATUS: {r['status']}")
    print("---")

# Also check event counts per hypothesis
print("\n\n=== EVENT COUNTS ===")
cur.execute(f"SELECT hypothesis_name, COUNT(*) as cnt FROM events WHERE hypothesis_name IN ({placeholders}) GROUP BY hypothesis_name ORDER BY cnt", names)
for r in cur.fetchall():
    print(f"{r[0]}: {r[1]} events")

# Check if events overlap between pairs
pairs = [
    ('nba_bubble_desperation_spread_value', 'nba_bubble_underdog_motivation_spread'),
    ('nba_blowout_fav_spread_regression', 'nba_dominant_win_hangover_spread'),
    ('nba_elite_sandwich_letdown_ats', 'nba_elite_sandwich_coasting_spread'),
]

print("\n\n=== EVENT OVERLAP ANALYSIS ===")
for a, b in pairs:
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT game_id FROM events WHERE hypothesis_name = ?
            INTERSECT
            SELECT game_id FROM events WHERE hypothesis_name = ?
        )
    """, (a, b))
    overlap = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM events WHERE hypothesis_name = ?", (a,))
    cnt_a = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM events WHERE hypothesis_name = ?", (b,))
    cnt_b = cur.fetchone()[0]

    print(f"{a} ({cnt_a}) vs {b} ({cnt_b}): {overlap} overlapping game_ids")

conn.close()
