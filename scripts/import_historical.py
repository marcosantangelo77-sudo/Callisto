"""
Historical odds import pipeline — downloads FREE data and loads into callisto.db.

Sources:
  1. AusSportsBetting.com — NFL historical odds (Excel, decimal odds, 2006-present)
  2. Local SBR-format Excel files — NBA/NFL (if placed in data/ directory)
  3. Basketball-Reference — NBA scores + schedule (no odds, but populates game data)

The normalized JSON matches what HistoricalOddsFetcher expects in response_json.
"""

import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from io import BytesIO, StringIO
from pathlib import Path

import httpx
import pandas as pd

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "memory" / "callisto.db"
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Team-name normalisation
# ---------------------------------------------------------------------------
NBA_TEAM_MAP = {
    # Common abbreviations / alternate names → canonical
    "LAL": "Los Angeles Lakers", "Lakers": "Los Angeles Lakers",
    "LAC": "Los Angeles Clippers", "Clippers": "Los Angeles Clippers",
    "GSW": "Golden State Warriors", "Warriors": "Golden State Warriors",
    "GS": "Golden State Warriors",
    "BOS": "Boston Celtics", "Celtics": "Boston Celtics",
    "NYK": "New York Knicks", "Knicks": "New York Knicks",
    "NY": "New York Knicks",
    "BKN": "Brooklyn Nets", "Nets": "Brooklyn Nets",
    "BRK": "Brooklyn Nets",
    "PHI": "Philadelphia 76ers", "76ers": "Philadelphia 76ers",
    "TOR": "Toronto Raptors", "Raptors": "Toronto Raptors",
    "CHI": "Chicago Bulls", "Bulls": "Chicago Bulls",
    "CLE": "Cleveland Cavaliers", "Cavaliers": "Cleveland Cavaliers",
    "DET": "Detroit Pistons", "Pistons": "Detroit Pistons",
    "IND": "Indiana Pacers", "Pacers": "Indiana Pacers",
    "MIL": "Milwaukee Bucks", "Bucks": "Milwaukee Bucks",
    "ATL": "Atlanta Hawks", "Hawks": "Atlanta Hawks",
    "CHA": "Charlotte Hornets", "Hornets": "Charlotte Hornets",
    "CHO": "Charlotte Hornets", "CHH": "Charlotte Hornets",
    "MIA": "Miami Heat", "Heat": "Miami Heat",
    "ORL": "Orlando Magic", "Magic": "Orlando Magic",
    "WAS": "Washington Wizards", "Wizards": "Washington Wizards",
    "DAL": "Dallas Mavericks", "Mavericks": "Dallas Mavericks",
    "HOU": "Houston Rockets", "Rockets": "Houston Rockets",
    "MEM": "Memphis Grizzlies", "Grizzlies": "Memphis Grizzlies",
    "NOP": "New Orleans Pelicans", "Pelicans": "New Orleans Pelicans",
    "NO": "New Orleans Pelicans",
    "SAS": "San Antonio Spurs", "Spurs": "San Antonio Spurs",
    "SA": "San Antonio Spurs",
    "DEN": "Denver Nuggets", "Nuggets": "Denver Nuggets",
    "MIN": "Minnesota Timberwolves", "Timberwolves": "Minnesota Timberwolves",
    "OKC": "Oklahoma City Thunder", "Thunder": "Oklahoma City Thunder",
    "POR": "Portland Trail Blazers", "Trail Blazers": "Portland Trail Blazers",
    "Blazers": "Portland Trail Blazers",
    "UTA": "Utah Jazz", "Jazz": "Utah Jazz",
    "SAC": "Sacramento Kings", "Kings": "Sacramento Kings",
    "PHX": "Phoenix Suns", "Suns": "Phoenix Suns",
    "PHO": "Phoenix Suns",
    "SEA": "Seattle SuperSonics",
    # Historical
    "NJN": "New Jersey Nets", "New Jersey": "New Jersey Nets",
    "NOH": "New Orleans Hornets",
    "VAN": "Vancouver Grizzlies",
}

NFL_TEAM_MAP = {
    "ARI": "Arizona Cardinals", "Arizona Cardinals": "Arizona Cardinals",
    "ATL": "Atlanta Falcons", "Atlanta Falcons": "Atlanta Falcons",
    "BAL": "Baltimore Ravens", "Baltimore Ravens": "Baltimore Ravens",
    "BUF": "Buffalo Bills", "Buffalo Bills": "Buffalo Bills",
    "CAR": "Carolina Panthers", "Carolina Panthers": "Carolina Panthers",
    "CHI": "Chicago Bears", "Chicago Bears": "Chicago Bears",
    "CIN": "Cincinnati Bengals", "Cincinnati Bengals": "Cincinnati Bengals",
    "CLE": "Cleveland Browns", "Cleveland Browns": "Cleveland Browns",
    "DAL": "Dallas Cowboys", "Dallas Cowboys": "Dallas Cowboys",
    "DEN": "Denver Broncos", "Denver Broncos": "Denver Broncos",
    "DET": "Detroit Lions", "Detroit Lions": "Detroit Lions",
    "GB": "Green Bay Packers", "Green Bay Packers": "Green Bay Packers",
    "HOU": "Houston Texans", "Houston Texans": "Houston Texans",
    "IND": "Indianapolis Colts", "Indianapolis Colts": "Indianapolis Colts",
    "JAX": "Jacksonville Jaguars", "Jacksonville Jaguars": "Jacksonville Jaguars",
    "JAC": "Jacksonville Jaguars",
    "KC": "Kansas City Chiefs", "Kansas City Chiefs": "Kansas City Chiefs",
    "LAC": "Los Angeles Chargers", "Los Angeles Chargers": "Los Angeles Chargers",
    "LAR": "Los Angeles Rams", "Los Angeles Rams": "Los Angeles Rams",
    "LV": "Las Vegas Raiders", "Las Vegas Raiders": "Las Vegas Raiders",
    "OAK": "Oakland Raiders", "Oakland Raiders": "Oakland Raiders",
    "MIA": "Miami Dolphins", "Miami Dolphins": "Miami Dolphins",
    "MIN": "Minnesota Vikings", "Minnesota Vikings": "Minnesota Vikings",
    "NE": "New England Patriots", "New England Patriots": "New England Patriots",
    "NO": "New Orleans Saints", "New Orleans Saints": "New Orleans Saints",
    "NYG": "New York Giants", "New York Giants": "New York Giants",
    "NYJ": "New York Jets", "New York Jets": "New York Jets",
    "PHI": "Philadelphia Eagles", "Philadelphia Eagles": "Philadelphia Eagles",
    "PIT": "Pittsburgh Steelers", "Pittsburgh Steelers": "Pittsburgh Steelers",
    "SEA": "Seattle Seahawks", "Seattle Seahawks": "Seattle Seahawks",
    "SF": "San Francisco 49ers", "San Francisco 49ers": "San Francisco 49ers",
    "TB": "Tampa Bay Buccaneers", "Tampa Bay Buccaneers": "Tampa Bay Buccaneers",
    "TEN": "Tennessee Titans", "Tennessee Titans": "Tennessee Titans",
    "WAS": "Washington Commanders", "Washington Commanders": "Washington Commanders",
    "Washington Football Team": "Washington Commanders",
    "Washington Redskins": "Washington Commanders",
    "St. Louis Rams": "Los Angeles Rams",
    "San Diego Chargers": "Los Angeles Chargers",
    "SD": "Los Angeles Chargers",
    "STL": "Los Angeles Rams",
}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def decimal_to_american(dec: float) -> int:
    """Convert decimal odds to American odds."""
    if dec is None or pd.isna(dec) or dec <= 1.0:
        return -110  # fallback
    if dec >= 2.0:
        return int(round((dec - 1) * 100))
    else:
        return int(round(-100 / (dec - 1)))


def american_to_implied(american: int) -> float:
    """Convert American odds to implied probability."""
    if american < 0:
        return abs(american) / (abs(american) + 100)
    else:
        return 100 / (american + 100)


def ensure_tables(conn: sqlite3.Connection):
    """Create tables if they don't exist."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS historical_odds_cache (
            cache_id INTEGER PRIMARY KEY AUTOINCREMENT,
            sport TEXT NOT NULL,
            snapshot_date TEXT NOT NULL,
            event_id TEXT,
            market_type TEXT,
            response_json TEXT NOT NULL,
            credits_cost INTEGER DEFAULT 0,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(sport, snapshot_date, event_id, market_type)
        )
    """)
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
            source TEXT DEFAULT 'aussportsbetting',
            UNIQUE(sport, game_date, home_team, away_team)
        )
    """)
    conn.commit()


def normalize_team(name: str, sport: str) -> str:
    """Normalize a team name to canonical form."""
    name = name.strip()
    mapping = NBA_TEAM_MAP if sport == "basketball_nba" else NFL_TEAM_MAP
    return mapping.get(name, name)


# ---------------------------------------------------------------------------
# AusSportsBetting NFL import
# ---------------------------------------------------------------------------

def download_aussportsbetting_nfl() -> Path:
    """Download NFL odds from aussportsbetting.com. Returns path to xlsx."""
    dest = DATA_DIR / "nfl_aussportsbetting.xlsx"
    if dest.exists():
        age_hours = (time.time() - dest.stat().st_mtime) / 3600
        if age_hours < 24 * 7:  # re-use if less than 7 days old
            print(f"  Using cached {dest.name} (age: {age_hours:.0f}h)")
            return dest

    print("  Downloading NFL data from aussportsbetting.com ...")
    client = httpx.Client(
        timeout=60,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )
    r = client.get("https://www.aussportsbetting.com/historical_data/nfl.xlsx")
    r.raise_for_status()
    dest.write_bytes(r.content)
    print(f"  Downloaded {len(r.content):,} bytes -> {dest.name}")
    return dest


def parse_aussportsbetting(filepath: Path, sport: str, min_year: int = 2023) -> list[dict]:
    """
    Parse AusSportsBetting Excel file.
    Returns list of per-date game groups ready for insertion.

    Columns in the file:
      Date, Home Team, Away Team, Home Score, Away Score, Overtime?,
      Playoff Game?, Neutral Venue?,
      Home Odds Open/Min/Max/Close, Away Odds Open/Min/Max/Close,
      Home Line Open/Min/Max/Close, Away Line Open/Min/Max/Close,
      Home Line Odds Open/Min/Max/Close, Away Line Odds Open/Min/Max/Close,
      Total Score Open/Min/Max/Close,
      Total Score Over Open/Min/Max/Close, Total Score Under Open/Min/Max/Close,
      Notes
    """
    print(f"  Parsing {filepath.name} ...")
    df = pd.read_excel(filepath)
    df["Date"] = pd.to_datetime(df["Date"])

    # Filter to recent seasons
    df = df[df["Date"].dt.year >= min_year].copy()
    print(f"  {len(df)} games from {min_year} onward")

    # Group by date
    games_by_date: dict[str, list[dict]] = {}
    for _, row in df.iterrows():
        date_str = row["Date"].strftime("%Y-%m-%d")
        home = normalize_team(str(row["Home Team"]), sport)
        away = normalize_team(str(row["Away Team"]), sport)

        # Odds (decimal → American)
        home_ml_close = decimal_to_american(row.get("Home Odds Close"))
        away_ml_close = decimal_to_american(row.get("Away Odds Close"))

        # Spread (from home perspective)
        home_spread = row.get("Home Line Close")
        if pd.isna(home_spread):
            home_spread = row.get("Home Line Open")
        if pd.isna(home_spread):
            home_spread = None

        # Spread odds (decimal → American)
        home_spread_odds = decimal_to_american(row.get("Home Line Odds Close"))
        away_spread_odds = decimal_to_american(row.get("Away Line Odds Close"))

        # Totals
        total_close = row.get("Total Score Close")
        if pd.isna(total_close):
            total_close = row.get("Total Score Open")
        if pd.isna(total_close):
            total_close = None

        over_odds = decimal_to_american(row.get("Total Score Over Close"))
        under_odds = decimal_to_american(row.get("Total Score Under Close"))

        # Scores
        home_score = int(row["Home Score"]) if not pd.isna(row["Home Score"]) else None
        away_score = int(row["Away Score"]) if not pd.isna(row["Away Score"]) else None

        # Build markets
        markets = []

        # H2H (moneyline)
        if home_ml_close and away_ml_close:
            markets.append({
                "key": "h2h",
                "outcomes": [
                    {"name": home, "price": home_ml_close},
                    {"name": away, "price": away_ml_close},
                ],
            })

        # Spreads
        if home_spread is not None:
            away_spread = -home_spread if home_spread else 0
            markets.append({
                "key": "spreads",
                "outcomes": [
                    {"name": home, "price": home_spread_odds, "point": float(home_spread)},
                    {"name": away, "price": away_spread_odds, "point": float(away_spread)},
                ],
            })

        # Totals
        if total_close is not None:
            markets.append({
                "key": "totals",
                "outcomes": [
                    {"name": "Over", "price": over_odds, "point": float(total_close)},
                    {"name": "Under", "price": under_odds, "point": float(total_close)},
                ],
            })

        game = {
            "home_team": home,
            "away_team": away,
            "commence_time": f"{date_str}T19:00:00Z",
            "home_score": home_score,
            "away_score": away_score,
            "bookmakers": [
                {
                    "key": "consensus",
                    "title": "Consensus",
                    "markets": markets,
                }
            ],
        }

        games_by_date.setdefault(date_str, []).append(game)

    return games_by_date


# ---------------------------------------------------------------------------
# SBR-format Excel import (for local files)
# ---------------------------------------------------------------------------

def parse_sbr_excel(filepath: Path, sport: str) -> dict[str, list[dict]]:
    """
    Parse SBR-format Excel files.
    Columns: Date, Rot, VH, Team, 1st, 2nd, 3rd, 4th, Final, Open, Close, ML, 2H
    Rows come in pairs: visitor row then home row.
    """
    print(f"  Parsing SBR file: {filepath.name} ...")

    # Determine engine based on extension
    ext = filepath.suffix.lower()
    engine = "xlrd" if ext == ".xls" else "openpyxl"

    try:
        df = pd.read_excel(filepath, engine=engine)
    except Exception as e:
        print(f"  ERROR reading {filepath.name}: {e}")
        return {}

    # Normalise column names
    df.columns = [str(c).strip() for c in df.columns]

    # Common SBR column patterns
    col_map = {}
    for c in df.columns:
        cl = c.lower()
        if cl in ("date",):
            col_map["date"] = c
        elif cl in ("vh", "v/h"):
            col_map["vh"] = c
        elif cl in ("team",):
            col_map["team"] = c
        elif cl in ("final",):
            col_map["final"] = c
        elif cl in ("open",):
            col_map["open"] = c
        elif cl in ("close",):
            col_map["close"] = c
        elif cl in ("ml", "money line", "moneyline"):
            col_map["ml"] = c
        elif cl in ("1st",):
            col_map["1st"] = c
        elif cl in ("2nd",):
            col_map["2nd"] = c
        elif cl in ("3rd",):
            col_map["3rd"] = c
        elif cl in ("4th",):
            col_map["4th"] = c
        elif cl in ("ou",):
            col_map["ou"] = c

    if "date" not in col_map or "team" not in col_map:
        print(f"  SKIP {filepath.name}: missing required columns (found: {list(df.columns)})")
        return {}

    games_by_date: dict[str, list[dict]] = {}
    i = 0
    while i < len(df) - 1:
        row1 = df.iloc[i]
        row2 = df.iloc[i + 1]

        # Determine which is visitor, which is home
        vh1 = str(row1.get(col_map.get("vh", ""), "V")).strip().upper()
        vh2 = str(row2.get(col_map.get("vh", ""), "H")).strip().upper()

        if vh1 == "V" and vh2 == "H":
            visitor_row, home_row = row1, row2
        elif vh1 == "H" and vh2 == "V":
            home_row, visitor_row = row1, row2
        else:
            # Assume pairs: first = visitor, second = home
            visitor_row, home_row = row1, row2

        # Parse date
        try:
            raw_date = home_row[col_map["date"]]
            if isinstance(raw_date, (int, float)) and not pd.isna(raw_date):
                # SBR sometimes uses MMDD format
                raw_date = str(int(raw_date)).zfill(4)
                month = int(raw_date[:2])
                day = int(raw_date[2:])
                # Guess year from filename
                year_match = re.search(r"20\d{2}", filepath.stem)
                year = int(year_match.group()) if year_match else 2024
                # NBA season spans two years
                if month >= 10:
                    pass  # correct year
                else:
                    year += 1  # second half of season
                date_str = f"{year}-{month:02d}-{day:02d}"
            elif isinstance(raw_date, str):
                date_str = pd.to_datetime(raw_date).strftime("%Y-%m-%d")
            else:
                date_str = pd.to_datetime(raw_date).strftime("%Y-%m-%d")
        except Exception:
            i += 2
            continue

        home = normalize_team(str(home_row[col_map["team"]]).strip(), sport)
        away = normalize_team(str(visitor_row[col_map["team"]]).strip(), sport)

        # Scores
        home_score = None
        away_score = None
        if "final" in col_map:
            try:
                home_score = int(float(home_row[col_map["final"]]))
                away_score = int(float(visitor_row[col_map["final"]]))
            except (ValueError, TypeError):
                pass

        # Spread (close line)
        home_spread = None
        if "close" in col_map:
            try:
                home_spread = float(home_row[col_map["close"]])
            except (ValueError, TypeError):
                pass
        if home_spread is None and "open" in col_map:
            try:
                home_spread = float(home_row[col_map["open"]])
            except (ValueError, TypeError):
                pass

        # Moneyline
        home_ml = None
        away_ml = None
        if "ml" in col_map:
            try:
                home_ml = int(float(home_row[col_map["ml"]]))
                away_ml = int(float(visitor_row[col_map["ml"]]))
            except (ValueError, TypeError):
                pass

        # Build markets
        markets = []
        if home_ml is not None and away_ml is not None:
            markets.append({
                "key": "h2h",
                "outcomes": [
                    {"name": home, "price": home_ml},
                    {"name": away, "price": away_ml},
                ],
            })
        if home_spread is not None:
            markets.append({
                "key": "spreads",
                "outcomes": [
                    {"name": home, "price": -110, "point": home_spread},
                    {"name": away, "price": -110, "point": -home_spread},
                ],
            })

        # Over/Under — SBR format sometimes has it in a separate column
        # or encoded in the close/open field of the visitor row
        if "close" in col_map:
            try:
                visitor_close = float(visitor_row[col_map["close"]])
                # If the visitor "close" looks like a total (> 150 for NBA, > 30 for NFL)
                threshold = 150 if sport == "basketball_nba" else 30
                if visitor_close > threshold:
                    markets.append({
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": visitor_close},
                            {"name": "Under", "price": -110, "point": visitor_close},
                        ],
                    })
            except (ValueError, TypeError):
                pass

        game = {
            "home_team": home,
            "away_team": away,
            "commence_time": f"{date_str}T19:00:00Z",
            "home_score": home_score,
            "away_score": away_score,
            "bookmakers": [
                {
                    "key": "consensus",
                    "title": "Consensus",
                    "markets": markets,
                }
            ],
        }
        games_by_date.setdefault(date_str, []).append(game)
        i += 2

    return games_by_date


# ---------------------------------------------------------------------------
# Basketball-Reference NBA scores scraper
# ---------------------------------------------------------------------------

def scrape_bref_nba_season(season_end_year: int) -> dict[str, list[dict]]:
    """
    Scrape basketball-reference schedule/results for an NBA season.
    season_end_year=2025 means the 2024-25 season.
    Returns games_by_date dict with scores only (no odds).
    """
    print(f"  Scraping Basketball-Reference for {season_end_year-1}-{str(season_end_year)[-2:]} season ...")
    url = f"https://www.basketball-reference.com/leagues/NBA_{season_end_year}_games.html"

    client = httpx.Client(
        timeout=30,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
    )

    games_by_date: dict[str, list[dict]] = {}
    total_games = 0

    # basketball-reference splits schedule into monthly pages
    # First, get the main page which has month links
    try:
        r = client.get(url)
        r.raise_for_status()
    except Exception as e:
        print(f"  ERROR fetching {url}: {e}")
        return {}

    # Parse all tables from the page
    try:
        tables = pd.read_html(StringIO(r.text))
    except Exception as e:
        print(f"  ERROR parsing HTML: {e}")
        return {}

    # Also try monthly sub-pages
    months = []
    for m in ["october", "november", "december", "january", "february",
              "march", "april", "may", "june"]:
        month_url = f"https://www.basketball-reference.com/leagues/NBA_{season_end_year}_games-{m}.html"
        try:
            time.sleep(3.5)  # respect rate limits (bref is strict)
            r = client.get(month_url)
            if r.status_code == 200:
                month_tables = pd.read_html(StringIO(r.text))
                tables.extend(month_tables)
                months.append(m)
        except Exception as e:
            print(f"    WARN: Failed to fetch {m}: {e}")

    if months:
        print(f"    Fetched months: {', '.join(months)}")

    for t in tables:
        cols = [str(c) for c in t.columns]
        # Look for the schedule table
        if "Date" not in cols:
            continue

        # Find column indices
        date_col = "Date"
        visitor_col = next((c for c in cols if "visitor" in c.lower() or "away" in c.lower()), None)
        home_col = next((c for c in cols if "home" in c.lower()), None)

        # Points columns are typically "PTS" and "PTS.1"
        pts_cols = [c for c in cols if c.startswith("PTS")]

        if not visitor_col or not home_col or len(pts_cols) < 2:
            continue

        for _, row in t.iterrows():
            try:
                date_raw = str(row[date_col])
                if "Date" in date_raw or "Playoffs" in date_raw:
                    continue  # skip header/section rows

                game_date = pd.to_datetime(date_raw)
                date_str = game_date.strftime("%Y-%m-%d")
                away = normalize_team(str(row[visitor_col]).strip(), "basketball_nba")
                home = normalize_team(str(row[home_col]).strip(), "basketball_nba")

                away_score = int(float(row[pts_cols[0]])) if not pd.isna(row[pts_cols[0]]) else None
                home_score = int(float(row[pts_cols[1]])) if not pd.isna(row[pts_cols[1]]) else None

                if home_score is None or away_score is None:
                    continue  # game hasn't been played yet

                game = {
                    "home_team": home,
                    "away_team": away,
                    "commence_time": f"{date_str}T19:00:00Z",
                    "home_score": home_score,
                    "away_score": away_score,
                    "bookmakers": [],  # no odds from bref
                }
                games_by_date.setdefault(date_str, []).append(game)
                total_games += 1
            except Exception:
                continue

    print(f"    Found {total_games} completed games across {len(games_by_date)} dates")
    return games_by_date


# ---------------------------------------------------------------------------
# Database insertion
# ---------------------------------------------------------------------------

def insert_odds_data(
    conn: sqlite3.Connection,
    sport: str,
    games_by_date: dict[str, list[dict]],
    source: str,
    market_type: str = "h2h,spreads,totals",
):
    """Insert games grouped by date into historical_odds_cache."""
    inserted = 0
    skipped = 0

    for date_str, games in sorted(games_by_date.items()):
        # Filter games that actually have odds
        games_with_odds = [g for g in games if g.get("bookmakers")]

        response_json = {
            "sport": sport,
            "date": date_str,
            "games": games,
            "game_count": len(games),
            "source": source,
        }

        try:
            conn.execute(
                """INSERT OR IGNORE INTO historical_odds_cache
                   (sport, snapshot_date, event_id, market_type, response_json, credits_cost)
                   VALUES (?, ?, NULL, ?, ?, 0)""",
                (sport, date_str, market_type, json.dumps(response_json)),
            )
            if conn.total_changes:
                inserted += 1
            else:
                skipped += 1
        except sqlite3.IntegrityError:
            skipped += 1

    conn.commit()
    return inserted, skipped


def insert_game_results(
    conn: sqlite3.Connection,
    sport: str,
    games_by_date: dict[str, list[dict]],
    source: str,
):
    """Insert game results into game_results table."""
    inserted = 0
    for date_str, games in games_by_date.items():
        for g in games:
            home_score = g.get("home_score")
            away_score = g.get("away_score")
            if home_score is None or away_score is None:
                continue

            total_score = home_score + away_score
            spread_result = float(away_score - home_score)  # positive = away won by more
            winner = g["home_team"] if home_score > away_score else (
                g["away_team"] if away_score > home_score else "push"
            )

            try:
                conn.execute(
                    """INSERT OR IGNORE INTO game_results
                       (sport, game_date, home_team, away_team, home_score, away_score,
                        total_score, spread_result, winner, source)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (sport, date_str, g["home_team"], g["away_team"],
                     home_score, away_score, total_score, spread_result, winner, source),
                )
                inserted += 1
            except sqlite3.IntegrityError:
                pass

    conn.commit()
    return inserted


# ---------------------------------------------------------------------------
# Main import orchestrator
# ---------------------------------------------------------------------------

def import_nfl_aussportsbetting(conn: sqlite3.Connection, min_year: int = 2023):
    """Download and import NFL data from aussportsbetting.com."""
    print("\n=== NFL: AusSportsBetting.com ===")
    try:
        filepath = download_aussportsbetting_nfl()
        games_by_date = parse_aussportsbetting(filepath, "americanfootball_nfl", min_year=min_year)
        total_games = sum(len(g) for g in games_by_date.values())
        print(f"  Parsed {total_games} games across {len(games_by_date)} dates")

        ins_odds, skip_odds = insert_odds_data(
            conn, "americanfootball_nfl", games_by_date, "aussportsbetting"
        )
        print(f"  Odds cache: {ins_odds} inserted, {skip_odds} already existed")

        ins_results = insert_game_results(
            conn, "americanfootball_nfl", games_by_date, "aussportsbetting"
        )
        print(f"  Game results: {ins_results} inserted")
    except Exception as e:
        print(f"  ERROR: {e}")


def import_local_sbr_files(conn: sqlite3.Connection):
    """Import any local SBR-format Excel files from data/ directory."""
    print("\n=== Local SBR Excel Files ===")
    patterns = [
        ("nba", "basketball_nba"),
        ("nfl", "americanfootball_nfl"),
    ]

    found_any = False
    for prefix, sport in patterns:
        for f in DATA_DIR.glob(f"*{prefix}*odds*.xls*"):
            if "aussportsbetting" in f.name.lower():
                continue  # handled separately
            found_any = True
            print(f"\n  Processing: {f.name}")
            try:
                games_by_date = parse_sbr_excel(f, sport)
                total = sum(len(g) for g in games_by_date.values())
                print(f"    Parsed {total} games across {len(games_by_date)} dates")

                ins_odds, skip = insert_odds_data(conn, sport, games_by_date, "sbr")
                print(f"    Odds: {ins_odds} inserted, {skip} skipped")

                ins_res = insert_game_results(conn, sport, games_by_date, "sbr")
                print(f"    Results: {ins_res} inserted")
            except Exception as e:
                print(f"    ERROR: {e}")

    if not found_any:
        print("  No local SBR files found in data/")
        print("  To import SBR data, place Excel files matching *nba*odds*.xlsx or *nfl*odds*.xlsx in data/")


def import_nba_bref(conn: sqlite3.Connection, seasons: list[int] = None):
    """Import NBA scores from Basketball-Reference."""
    if seasons is None:
        seasons = [2024, 2025, 2026]

    print("\n=== NBA: Basketball-Reference (scores only) ===")
    for season_year in seasons:
        try:
            games_by_date = scrape_bref_nba_season(season_year)
            if not games_by_date:
                print(f"  No data for {season_year-1}-{str(season_year)[-2:]} season")
                continue

            # Insert into game_results (scores) — NOT into historical_odds_cache
            # since basketball-reference has no odds data and scores-only records
            # block real multi-book odds from being written for those dates
            ins = insert_game_results(conn, "basketball_nba", games_by_date, "basketball-reference")
            print(f"  Results: {ins} inserted for {season_year-1}-{str(season_year)[-2:]}")

            # Be polite to bref
            if season_year != seasons[-1]:
                print("  Waiting 5s before next season ...")
                time.sleep(5)

        except Exception as e:
            print(f"  ERROR for season {season_year}: {e}")


def print_summary(conn: sqlite3.Connection):
    """Print summary of what's in the database."""
    print("\n" + "=" * 60)
    print("DATABASE SUMMARY")
    print("=" * 60)

    cursor = conn.execute("""
        SELECT sport, market_type, COUNT(*) as entries,
               MIN(snapshot_date) as earliest, MAX(snapshot_date) as latest
        FROM historical_odds_cache
        GROUP BY sport, market_type
        ORDER BY sport, market_type
    """)
    print("\nhistorical_odds_cache:")
    for row in cursor.fetchall():
        print(f"  {row[0]:30s} | {row[1]:25s} | {row[2]:5d} entries | {row[3]} to {row[4]}")

    cursor = conn.execute("""
        SELECT sport, source, COUNT(*) as games,
               MIN(game_date) as earliest, MAX(game_date) as latest
        FROM game_results
        GROUP BY sport, source
        ORDER BY sport, source
    """)
    print("\ngame_results:")
    for row in cursor.fetchall():
        print(f"  {row[0]:30s} | {row[1]:25s} | {row[2]:5d} games | {row[3]} to {row[4]}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    import argparse

    parser = argparse.ArgumentParser(description="Import historical odds data into Callisto DB")
    parser.add_argument("--nfl", action="store_true", help="Import NFL from aussportsbetting")
    parser.add_argument("--nba", action="store_true", help="Import NBA scores from basketball-reference")
    parser.add_argument("--sbr", action="store_true", help="Import local SBR Excel files from data/")
    parser.add_argument("--all", action="store_true", help="Import everything")
    parser.add_argument("--min-year", type=int, default=2023, help="Minimum year to import (default: 2023)")
    parser.add_argument("--nba-seasons", type=int, nargs="+", default=[2024, 2025, 2026],
                        help="NBA seasons to scrape (by end year, e.g. 2025 = 2024-25)")
    parser.add_argument("--db", type=str, default=str(DB_PATH), help="Database path")
    args = parser.parse_args()

    if not any([args.nfl, args.nba, args.sbr, args.all]):
        args.all = True

    db_path = Path(args.db)
    print(f"Database: {db_path}")
    conn = sqlite3.connect(str(db_path))
    ensure_tables(conn)

    try:
        if args.all or args.nfl:
            import_nfl_aussportsbetting(conn, min_year=args.min_year)

        if args.all or args.sbr:
            import_local_sbr_files(conn)

        if args.all or args.nba:
            import_nba_bref(conn, seasons=args.nba_seasons)

        print_summary(conn)

    finally:
        conn.close()

    print("\nDone!")


if __name__ == "__main__":
    main()
