import sqlite3, json

conn = sqlite3.connect('memory/callisto.db')
conn.row_factory = sqlite3.Row

row = conn.execute("SELECT snapshot_json FROM odds_snapshots WHERE sport LIKE '%nba%' ORDER BY timestamp DESC LIMIT 1").fetchone()
data = json.loads(row['snapshot_json'])
games = data.get('games', data) if isinstance(data, dict) else data

for game in games:
    if isinstance(game, str):
        game = json.loads(game)
    if not isinstance(game, dict):
        continue
    game_str = json.dumps(game).lower()
    if 'thunder' not in game_str and 'celtics' not in game_str:
        continue

    print(f"Game: {game.get('away_team')} @ {game.get('home_team')}")
    print(f"Start: {game.get('commence_time')}")
    print(f"Game ID: {game.get('id')}")

    bookmakers = game.get('bookmakers', [])
    print(f"\n{'Book':25s} {'OKC ML':>8s} {'BOS ML':>8s}")
    print("-" * 45)

    okc_lines = []
    bos_lines = []

    for bm in sorted(bookmakers, key=lambda x: x.get('title', '')):
        bm_name = bm.get('title', bm.get('key', '?'))
        for mkt in bm.get('markets', []):
            if mkt.get('key') == 'h2h':
                outcomes = {o.get('name'): o.get('price') for o in mkt.get('outcomes', [])}
                okc = outcomes.get('Oklahoma City Thunder', '?')
                bos = outcomes.get('Boston Celtics', '?')
                print(f"  {bm_name:23s} {str(okc):>8s} {str(bos):>8s}")
                if isinstance(okc, (int, float)):
                    okc_lines.append((bm_name, okc))
                if isinstance(bos, (int, float)):
                    bos_lines.append((bm_name, bos))

    print(f"\nBest OKC ML: {max(okc_lines, key=lambda x: x[1]) if okc_lines else 'N/A'}")
    print(f"Best BOS ML: {max(bos_lines, key=lambda x: x[1]) if bos_lines else 'N/A'}")

    # Check for DraftKings and Fanatics specifically
    print("\n--- DraftKings & Fanatics ---")
    for bm in bookmakers:
        bm_name = bm.get('title', bm.get('key', ''))
        if 'draftkings' in bm_name.lower() or 'fanatics' in bm_name.lower() or 'fanduel' in bm_name.lower():
            for mkt in bm.get('markets', []):
                if mkt.get('key') == 'h2h':
                    outcomes = {o.get('name'): o.get('price') for o in mkt.get('outcomes', [])}
                    print(f"  {bm_name}: OKC {outcomes.get('Oklahoma City Thunder', 'N/A')}, BOS {outcomes.get('Boston Celtics', 'N/A')}")

    # Check PointsBet
    print("\n--- PointsBet ---")
    found_pb = False
    for bm in bookmakers:
        bm_name = bm.get('title', bm.get('key', ''))
        if 'pointsbet' in bm_name.lower():
            found_pb = True
            for mkt in bm.get('markets', []):
                if mkt.get('key') == 'h2h':
                    outcomes = {o.get('name'): o.get('price') for o in mkt.get('outcomes', [])}
                    print(f"  {bm_name}: OKC {outcomes.get('Oklahoma City Thunder', 'N/A')}, BOS {outcomes.get('Boston Celtics', 'N/A')}")
    if not found_pb:
        print("  NOT FOUND in snapshot")

conn.close()
