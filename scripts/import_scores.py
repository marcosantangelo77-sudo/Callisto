"""
Game results import pipeline — pulls actual scores from ESPN API.

Populates the game_results table so backtests can validate predictions
against actual outcomes (ATS results, totals, moneyline winners).

ESPN API is free, no key required, but we rate-limit ourselves to be polite.
"""

import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "memory" / "callisto.db"

# ---------------------------------------------------------------------------
# ESPN API
# ---------------------------------------------------------------------------
ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"

SPORT_CONFIGS = {
    "basketball_nba": {
        "espn_path": "basketball/nba",
        "season_start_month": 10,   # October
        "season_end_month": 6,      # June (playoffs)
        "games_per_day": 15,        # max games on a busy day
    },
    "americanfootball_nfl": {
        "espn_path": "football/nfl",
        "season_start_month": 9,    # September
        "season_end_month": 2,      # February (Super Bowl)
        "games_per_day": 16,
    },
}

# ---------------------------------------------------------------------------
# Team name normalisation (ESPN names → canonical)
# ---------------------------------------------------------------------------
ESPN_TEAM_NORMALISE = {
    # NBA
    "LA Clippers": "Los Angeles Clippers",
    "LA Lakers": "Los Angeles Lakers",
    # NFL
    "Washington Football Team": "Washington Commanders",
    "Washington Redskins": "Washington Commanders",
    "Oakland Raiders": "Las Vegas Raiders",
    "San Diego Chargers": "Los Angeles Chargers",
    "St. Louis Rams": "Los Angeles Rams",
}


def normalize_espn_team(name: str) -> str:
    return ESPN_TEAM_NORMALISE.get(name, name)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def ensure_tables(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS game_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            game_date TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            home_score INTEGER,
            away_score INTEGER,
            total_score INTEGER,
            spread_result REAL,
            winner TEXT,
            source TEXT DEFAULT 'espn',
            UNIQUE(sport, game_date, home_team, away_team)
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# ESPN Fetcher
# ---------------------------------------------------------------------------

def fetch_espn_scoreboard(sport: str, date_str: str, client: httpx.Client) -> list[dict]:
    """
    Fetch scoreboard for a specific date from ESPN API.
    Returns list of parsed game dicts.
    """
    config = SPORT_CONFIGS.get(sport)
    if not config:
        return []

    espn_date = date_str.replace("-", "")  # ESPN wants YYYYMMDD
    url = f"{ESPN_BASE}/{config['espn_path']}/scoreboard"
    params = {"dates": espn_date}

    try:
        r = client.get(url, params=params)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"    ERROR fetching {date_str}: {e}")
        return []

    games = []
    for event in data.get("events", []):
        competitions = event.get("competitions", [])
        if not competitions:
            continue

        comp = competitions[0]
        competitors = comp.get("competitors", [])
        if len(competitors) < 2:
            continue

        home = None
        away = None
        for c in competitors:
            team_info = {
                "name": normalize_espn_team(c.get("team", {}).get("displayName", "Unknown")),
                "score": None,
                "is_home": c.get("homeAway") == "home",
            }
            try:
                team_info["score"] = int(c.get("score", 0))
            except (ValueError, TypeError):
                pass

            if team_info["is_home"]:
                home = team_info
            else:
                away = team_info

        if not home or not away:
            continue

        # Check if game is completed
        status = comp.get("status", {}).get("type", {}).get("completed", False)
        if not status:
            continue

        games.append({
            "home_team": home["name"],
            "away_team": away["name"],
            "home_score": home["score"],
            "away_score": away["score"],
        })

    return games


def generate_date_range(
    sport: str,
    season_end_year: int,
) -> list[str]:
    """Generate the list of dates for a season."""
    config = SPORT_CONFIGS[sport]

    if sport == "basketball_nba":
        # NBA season: Oct of year-1 through June of year
        start = datetime(season_end_year - 1, config["season_start_month"], 1)
        end = datetime(season_end_year, config["season_end_month"], 30)
    elif sport == "americanfootball_nfl":
        # NFL season: Sep through Feb
        start = datetime(season_end_year - 1, config["season_start_month"], 1)
        end = datetime(season_end_year, config["season_end_month"], 28)
    else:
        return []

    dates = []
    current = start
    while current <= end:
        dates.append(current.strftime("%Y-%m-%d"))
        current += timedelta(days=1)
    return dates


def import_season_scores(
    conn: sqlite3.Connection,
    sport: str,
    season_end_year: int,
    client: httpx.Client,
    skip_existing: bool = True,
):
    """Import all game results for a season from ESPN."""
    dates = generate_date_range(sport, season_end_year)
    print(f"  Season {season_end_year-1}-{str(season_end_year)[-2:]}: {len(dates)} dates to check")

    # If skip_existing, find which dates we already have
    existing_dates = set()
    if skip_existing:
        cursor = conn.execute(
            "SELECT DISTINCT game_date FROM game_results WHERE sport = ?",
            (sport,),
        )
        existing_dates = {row[0] for row in cursor.fetchall()}
        dates_to_fetch = [d for d in dates if d not in existing_dates]
        if len(dates_to_fetch) < len(dates):
            print(f"  Skipping {len(dates) - len(dates_to_fetch)} dates already in DB")
        dates = dates_to_fetch

    total_inserted = 0
    total_games = 0
    batch_count = 0

    for i, date_str in enumerate(dates):
        games = fetch_espn_scoreboard(sport, date_str, client)
        total_games += len(games)

        for g in games:
            if g["home_score"] is None or g["away_score"] is None:
                continue

            total_score = g["home_score"] + g["away_score"]
            spread_result = float(g["away_score"] - g["home_score"])
            winner = (
                g["home_team"] if g["home_score"] > g["away_score"]
                else g["away_team"] if g["away_score"] > g["home_score"]
                else "push"
            )

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO game_results
                       (sport, game_date, home_team, away_team, home_score, away_score,
                        total_score, spread_result, winner, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'espn')""",
                    (sport, date_str, g["home_team"], g["away_team"],
                     g["home_score"], g["away_score"], total_score, spread_result, winner),
                )
                total_inserted += 1
            except sqlite3.IntegrityError:
                pass

        batch_count += 1
        if batch_count >= 50:
            conn.commit()
            batch_count = 0

        # Progress every 30 dates
        if (i + 1) % 30 == 0:
            print(f"    Progress: {i+1}/{len(dates)} dates, {total_games} games found, {total_inserted} inserted")

        # Rate limit: ~2 requests/second
        time.sleep(0.5)

    conn.commit()
    print(f"  Season complete: {total_games} games found, {total_inserted} new insertions")
    return total_inserted


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Import game scores from ESPN API")
    parser.add_argument("--sport", choices=["nba", "nfl", "both"], default="both",
                        help="Which sport to import")
    parser.add_argument("--seasons", type=int, nargs="+", default=[2024, 2025, 2026],
                        help="Season end years (e.g. 2025 = 2024-25 season)")
    parser.add_argument("--no-skip", action="store_true",
                        help="Don't skip dates already in DB (re-fetch everything)")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="Database path")
    args = parser.parse_args()

    db_path = Path(args.db)
    print(f"Database: {db_path}")
    conn = sqlite3.connect(str(db_path))
    ensure_tables(conn)

    client = httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )

    sports = []
    if args.sport in ("nba", "both"):
        sports.append("basketball_nba")
    if args.sport in ("nfl", "both"):
        sports.append("americanfootball_nfl")

    try:
        for sport in sports:
            sport_label = "NBA" if "nba" in sport else "NFL"
            print(f"\n{'='*60}")
            print(f"  {sport_label} — ESPN Score Import")
            print(f"{'='*60}")

            for season in args.seasons:
                import_season_scores(
                    conn, sport, season, client,
                    skip_existing=not args.no_skip,
                )

        # Print summary
        print(f"\n{'='*60}")
        print("DATABASE SUMMARY — game_results")
        print(f"{'='*60}")
        cursor = conn.execute("""
            SELECT sport, source, COUNT(*) as games,
                   MIN(game_date) as earliest, MAX(game_date) as latest
            FROM game_results
            GROUP BY sport, source
            ORDER BY sport, source
        """)
        for row in cursor.fetchall():
            print(f"  {row[0]:30s} | {row[1]:10s} | {row[2]:5d} games | {row[3]} to {row[4]}")

    finally:
        client.close()
        conn.close()

    print("\nDone!")


if __name__ == "__main__":
    main()
