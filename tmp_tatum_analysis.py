import sqlite3

DB = 'C:/Users/marco/OneDrive/Desktop/Callisto/memory/callisto.db'
conn = sqlite3.connect(DB)
cur = conn.cursor()

# Check what teams Jrue Holiday, Porzingis, Horford are on
for name in ['Jrue Holiday', 'Kristaps Porzingis', 'Al Horford']:
    rows = cur.execute("""
        SELECT DISTINCT team FROM player_stats WHERE player_name = ?
    """, (name,)).fetchall()
    print(f'{name} teams: {[r[0] for r in rows]}')

    # Sample rows
    rows2 = cur.execute("""
        SELECT game_date, team, stat_type, stat_value FROM player_stats
        WHERE player_name = ? ORDER BY game_date DESC LIMIT 10
    """, (name,)).fetchall()
    for r in rows2:
        print(f'  {r}')

# Neemias Queta per-game (he's the center, relevant)
print('\n=== NEEMIAS QUETA PER-GAME ===')
from collections import defaultdict
tatum_events = set(r[0] for r in cur.execute("""
    SELECT DISTINCT event_id FROM player_stats WHERE player_name = 'Jayson Tatum'
""").fetchall())

nq_games = cur.execute("""
    SELECT event_id, game_date, stat_type, stat_value, minutes_played
    FROM player_stats
    WHERE player_name = 'Neemias Queta' AND team = 'Boston Celtics'
    AND stat_type IN ('points', 'assists', 'rebounds', 'steals', 'blocks', 'turnovers')
    ORDER BY game_date, stat_type
""").fetchall()

games = defaultdict(dict)
for eid, gd, st, sv, mins in nq_games:
    games[(gd, eid)]['minutes'] = mins
    games[(gd, eid)][st] = sv

print(f'{"date":<12} {"tatum":>6} {"pts":>5} {"reb":>5} {"ast":>5} {"stl":>5} {"blk":>5} {"tov":>5} {"min":>5}')
for (gd, eid), stats in sorted(games.items()):
    tag = 'YES' if eid in tatum_events else 'NO'
    pts = stats.get('points', '-')
    reb = stats.get('rebounds', '-')
    ast = stats.get('assists', '-')
    stl = stats.get('steals', '-')
    blk = stats.get('blocks', '-')
    tov = stats.get('turnovers', '-')
    mins = stats.get('minutes', '-')
    print(f'{gd:<12} {tag:>6} {pts:>5} {reb:>5} {ast:>5} {stl:>5} {blk:>5} {tov:>5} {mins:>5}')

# Dalano Banton per-game (he's getting minutes)
print('\n=== DALANO BANTON PER-GAME ===')
db_games = cur.execute("""
    SELECT event_id, game_date, stat_type, stat_value, minutes_played
    FROM player_stats
    WHERE player_name = 'Dalano Banton' AND team = 'Boston Celtics'
    AND stat_type IN ('points', 'assists', 'rebounds', 'steals', 'blocks', 'turnovers')
    ORDER BY game_date, stat_type
""").fetchall()

games2 = defaultdict(dict)
for eid, gd, st, sv, mins in db_games:
    games2[(gd, eid)]['minutes'] = mins
    games2[(gd, eid)][st] = sv

print(f'{"date":<12} {"tatum":>6} {"pts":>5} {"reb":>5} {"ast":>5} {"stl":>5} {"blk":>5} {"tov":>5} {"min":>5}')
for (gd, eid), stats in sorted(games2.items()):
    tag = 'YES' if eid in tatum_events else 'NO'
    pts = stats.get('points', '-')
    reb = stats.get('rebounds', '-')
    ast = stats.get('assists', '-')
    stl = stats.get('steals', '-')
    blk = stats.get('blocks', '-')
    tov = stats.get('turnovers', '-')
    mins = stats.get('minutes', '-')
    print(f'{gd:<12} {tag:>6} {pts:>5} {reb:>5} {ast:>5} {stl:>5} {blk:>5} {tov:>5} {mins:>5}')

conn.close()
