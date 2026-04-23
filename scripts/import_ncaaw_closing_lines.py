"""
Import NCAAW 2026 Tournament closing lines (markets + closing_lines_v2).

Source: FOX Sports + ESPN (DraftKings closing spreads/totals/moneylines)
Covers: First Four (Mar 18-19), First Round (Mar 20-21), Second Round (Mar 22-23)
Total: 52 games

Game results already exist in the DB from ESPN scraper (source='espn') with
full team names (e.g., "Nebraska Cornhuskers"). This script:
  1. Removes any duplicate game_results from prior foxsports_espn imports
  2. Imports markets and closing_lines_v2 using ESPN's full team names
  3. Uses DraftKings standard -110 juice for spreads/totals

Usage:
    python scripts/import_ncaaw_closing_lines.py
"""

import asyncio
import hashlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiosqlite
from tools.schema import ensure_schema, DB_PATH

SPORT = "basketball_ncaaw"
BOOK = "draftkings"

# Standard DraftKings spread/total juice
STANDARD_AMERICAN = -110
STANDARD_DECIMAL = 1.909

# ---- Team name mapping: short name -> ESPN full name ----
NAME_MAP = {
    "Alabama": "Alabama Crimson Tide",
    "Arizona State": "Arizona State Sun Devils",
    "Baylor": "Baylor Bears",
    "Cal Baptist": "California Baptist Lancers",
    "Charleston": "Charleston Cougars",
    "Clemson": "Clemson Tigers",
    "Colorado": "Colorado Buffaloes",
    "Colorado State": "Colorado State Rams",
    "Duke": "Duke Blue Devils",
    "Fairfield": "Fairfield Stags",
    "Fairleigh Dickinson": "Fairleigh Dickinson Knights",
    "Georgia": "Georgia Lady Bulldogs",
    "Gonzaga": "Gonzaga Bulldogs",
    "Green Bay": "Green Bay Phoenix",
    "High Point": "High Point Panthers",
    "Holy Cross": "Holy Cross Crusaders",
    "Howard": "Howard Bison",
    "Idaho": "Idaho Vandals",
    "Illinois": "Illinois Fighting Illini",
    "Iowa": "Iowa Hawkeyes",
    "Iowa State": "Iowa State Cyclones",
    "Jacksonville": "Jacksonville Dolphins",
    "James Madison": "James Madison Dukes",
    "Kentucky": "Kentucky Wildcats",
    "Louisville": "Louisville Cardinals",
    "LSU": "LSU Tigers",
    "Maryland": "Maryland Terrapins",
    "Miami OH": "Miami (OH) RedHawks",
    "Michigan": "Michigan Wolverines",
    "Michigan State": "Michigan State Spartans",
    "Minnesota": "Minnesota Golden Gophers",
    "Missouri State": "Missouri State Lady Bears",
    "Murray State": "Murray State Racers",
    "NC State": "NC State Wolfpack",
    "Nebraska": "Nebraska Cornhuskers",
    "Notre Dame": "Notre Dame Fighting Irish",
    "Ohio State": "Ohio State Buckeyes",
    "Oklahoma": "Oklahoma Sooners",
    "Oklahoma State": "Oklahoma State Cowgirls",
    "Ole Miss": "Ole Miss Rebels",
    "Oregon": "Oregon Ducks",
    "Princeton": "Princeton Tigers",
    "Rhode Island": "Rhode Island Rams",
    "Richmond": "Richmond Spiders",
    "Samford": "Samford Bulldogs",
    "South Carolina": "South Carolina Gamecocks",
    "South Dakota State": "South Dakota State Jackrabbits",
    "Southern": "Southern Jaguars",
    "Stephen F. Austin": "Stephen F. Austin Ladyjacks",
    "Syracuse": "Syracuse Orange",
    "TCU": "TCU Horned Frogs",
    "Tennessee": "Tennessee Lady Volunteers",
    "Texas": "Texas Longhorns",
    "Texas Tech": "Texas Tech Lady Raiders",
    "UC San Diego": "UC San Diego Tritons",
    "UConn": "UConn Huskies",
    "UCLA": "UCLA Bruins",
    "UNC": "North Carolina Tar Heels",
    "USC": "USC Trojans",
    "UTSA": "UTSA Roadrunners",
    "Vanderbilt": "Vanderbilt Commodores",
    "Vermont": "Vermont Catamounts",
    "Villanova": "Villanova Wildcats",
    "Virginia": "Virginia Cavaliers",
    "Virginia Tech": "Virginia Tech Hokies",
    "Washington": "Washington Huskies",
    "West Virginia": "West Virginia Mountaineers",
    "Western Illinois": "Western Illinois Leathernecks",
}


def n(short: str) -> str:
    """Resolve short name to ESPN full name."""
    return NAME_MAP.get(short, short)


def american_to_decimal(american: int) -> float:
    if american > 0:
        return 1.0 + (american / 100.0)
    else:
        return 1.0 + (100.0 / abs(american))


def make_market_id(date: str, away: str, home: str, market_type: str) -> str:
    slug = f"ncaaw_2026_{date}_{away}_{home}".lower().replace(" ", "_")
    return f"{slug}|{market_type}"


def make_event_id(date: str, away: str, home: str) -> str:
    raw = f"ncaaw_2026_{date}_{away}_{home}".lower().replace(" ", "_")
    return hashlib.md5(raw.encode()).hexdigest()[:12]


# ---- GAME DATA ----
# (date, away_short, home_short, spread_fav_short, spread_pts, total,
#  moneyline_fav, moneyline_dog)

GAMES = [
    # -- FIRST FOUR: March 18 --
    ("2026-03-18", "Richmond", "Nebraska", "Nebraska", 2.5, 138.5, -155, 130),
    ("2026-03-18", "Stephen F. Austin", "Missouri State", "Missouri State", 2.5, 138.5, -148, 124),

    # -- FIRST FOUR: March 19 --
    ("2026-03-19", "Arizona State", "Virginia", "Virginia", 2.5, 126.5, -148, 124),
    ("2026-03-19", "Samford", "Southern", "Southern", 2.5, 116.5, -155, 130),

    # -- FIRST ROUND: March 20 --
    ("2026-03-20", "Missouri State", "Texas", "Texas", 44.5, 134.5, None, None),
    ("2026-03-20", "Jacksonville", "LSU", "LSU", 51.5, 162.5, None, None),
    ("2026-03-20", "Holy Cross", "Michigan", "Michigan", 41.5, 130.5, None, None),
    ("2026-03-20", "UC San Diego", "TCU", "TCU", 34.5, 130.5, None, None),
    ("2026-03-20", "Charleston", "Duke", "Duke", 34.5, 131.5, None, None),
    ("2026-03-20", "Western Illinois", "UNC", "UNC", 25.5, 132.5, None, None),
    ("2026-03-20", "Green Bay", "Minnesota", "Minnesota", 21.5, 123.5, None, None),
    ("2026-03-20", "Idaho", "Oklahoma", "Oklahoma", 34.5, 158.5, None, None),
    ("2026-03-20", "Murray State", "Maryland", "Maryland", 30.5, 165.5, None, None),
    ("2026-03-20", "Gonzaga", "Ole Miss", "Ole Miss", 14.5, 135.5, None, None),
    ("2026-03-20", "Colorado State", "Michigan State", "Michigan State", 18.5, 133.5, None, None),
    ("2026-03-20", "South Dakota State", "Washington", "Washington", 5.5, 131.5, None, None),
    ("2026-03-20", "Nebraska", "Baylor", "Nebraska", 1.5, 139.5, None, None),
    ("2026-03-20", "Tennessee", "NC State", "NC State", 1.5, 151.5, None, None),
    ("2026-03-20", "Villanova", "Texas Tech", "Texas Tech", 1.5, 129.5, None, None),
    ("2026-03-20", "Virginia Tech", "Oregon", "Oregon", 3.5, 138.5, None, None),

    # -- FIRST ROUND: March 21 --
    ("2026-03-21", "Southern", "South Carolina", "South Carolina", 53.5, 129.5, None, None),
    ("2026-03-21", "UTSA", "UConn", "UConn", 55.5, 124.5, None, None),
    ("2026-03-21", "Cal Baptist", "UCLA", "UCLA", 51.5, 146.5, None, None),
    ("2026-03-21", "High Point", "Vanderbilt", "Vanderbilt", 36.5, 148.5, None, None),
    ("2026-03-21", "Fairleigh Dickinson", "Iowa", "Iowa", 31.5, 128.5, None, None),
    ("2026-03-21", "Howard", "Ohio State", "Ohio State", 38.5, 142.5, None, None),
    ("2026-03-21", "Vermont", "Louisville", "Louisville", 26.5, 121.5, None, None),
    ("2026-03-21", "Miami OH", "West Virginia", "West Virginia", 25.5, 124.5, None, None),
    ("2026-03-21", "James Madison", "Kentucky", "Kentucky", 15.5, 129.5, None, None),
    ("2026-03-21", "Fairfield", "Notre Dame", "Notre Dame", 11.5, 138.5, None, None),
    ("2026-03-21", "Rhode Island", "Alabama", "Alabama", 9.5, 120.5, None, None),
    ("2026-03-21", "Colorado", "Illinois", "Illinois", 3.5, 132.5, None, None),
    ("2026-03-21", "Virginia", "Georgia", "Georgia", 2.5, 131.5, None, None),
    ("2026-03-21", "Syracuse", "Iowa State", "Iowa State", 7.5, 149.5, None, None),
    ("2026-03-21", "Princeton", "Oklahoma State", "Oklahoma State", 6.5, 136.5, None, None),
    ("2026-03-21", "USC", "Clemson", "USC", 5.5, 120.5, None, None),

    # -- SECOND ROUND: March 22 --
    ("2026-03-22", "Oregon", "Texas", "Texas", 26.5, 136.5, None, None),
    ("2026-03-22", "NC State", "Michigan", "Michigan", 13.5, 143.5, None, None),
    ("2026-03-22", "Texas Tech", "LSU", "LSU", 24.5, 145.5, None, None),
    ("2026-03-22", "Baylor", "Duke", "Duke", 12.5, 126.5, None, None),
    ("2026-03-22", "Washington", "TCU", "TCU", 9.5, 125.5, None, None),
    ("2026-03-22", "Michigan State", "Oklahoma", "Oklahoma", 7.5, 158.5, None, None),
    ("2026-03-22", "Maryland", "UNC", "UNC", 2.5, 136.5, None, None),
    ("2026-03-22", "Ole Miss", "Minnesota", "Minnesota", 4.5, 126.5, None, None),

    # -- SECOND ROUND: March 23 --
    ("2026-03-23", "Syracuse", "UConn", "UConn", 35.5, 139.5, None, None),
    ("2026-03-23", "USC", "South Carolina", "South Carolina", 22.5, 132.5, None, None),
    ("2026-03-23", "Oklahoma State", "UCLA", "UCLA", 26.5, 138.5, None, None),
    ("2026-03-23", "Virginia", "Iowa", "Iowa", 13.5, 135.5, None, None),
    ("2026-03-23", "Illinois", "Vanderbilt", "Vanderbilt", 13.5, 152.5, None, None),
    ("2026-03-23", "Notre Dame", "Ohio State", "Ohio State", 5.5, 148.5, None, None),
    ("2026-03-23", "Alabama", "Louisville", "Louisville", 8.5, 132.5, None, None),
    ("2026-03-23", "Kentucky", "West Virginia", "West Virginia", 3.5, 128.5, None, None),
]


async def import_data():
    db_path = DB_PATH
    print(f"Database: {db_path}")
    await ensure_schema(db_path)

    async with aiosqlite.connect(db_path) as db:
        # Step 1: Remove duplicate game_results from prior foxsports_espn import
        deleted = await db.execute(
            "DELETE FROM game_results WHERE sport = ? AND source = 'foxsports_espn'",
            (SPORT,))
        print(f"Cleaned up {deleted.rowcount} prior foxsports_espn game_results")

        # Step 2: Clear prior markets and closing lines for re-import
        await db.execute(
            "DELETE FROM closing_lines_v2 WHERE market_id LIKE 'ncaaw_2026_%'")
        await db.execute(
            "DELETE FROM markets WHERE sport = ? AND market_id LIKE 'ncaaw_2026_%'",
            (SPORT,))

        inserted_markets = 0
        inserted_closing = 0

        for game in GAMES:
            (date, away_short, home_short, spread_fav_short,
             spread_pts, total, ml_fav, ml_dog) = game

            away = n(away_short)
            home = n(home_short)
            spread_fav = n(spread_fav_short)

            event_id = make_event_id(date, away, home)
            commence = f"{date}T19:00:00Z"

            for mtype in ["spreads", "totals", "h2h"]:
                mid = make_market_id(date, away, home, mtype)
                event_name = f"{away} vs {home}"

                await db.execute("""
                    INSERT OR REPLACE INTO markets
                        (market_id, sport, event_id, event_name,
                         home_team, away_team, commence_time, market_type)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, (mid, SPORT, event_id, event_name,
                      home, away, commence, mtype))
                inserted_markets += 1

                if mtype == "spreads":
                    # Home spread point: negative if home is favorite
                    home_point = -spread_pts if spread_fav == home else spread_pts
                    away_point = -home_point

                    await db.execute("""
                        INSERT OR REPLACE INTO closing_lines_v2
                            (market_id, book_id, outcome_name, point,
                             closing_price_american, closing_price_decimal,
                             is_last_change, recorded_at, reliable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (mid, BOOK, home, home_point,
                          STANDARD_AMERICAN, STANDARD_DECIMAL,
                          True, commence, True))

                    await db.execute("""
                        INSERT OR REPLACE INTO closing_lines_v2
                            (market_id, book_id, outcome_name, point,
                             closing_price_american, closing_price_decimal,
                             is_last_change, recorded_at, reliable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (mid, BOOK, away, away_point,
                          STANDARD_AMERICAN, STANDARD_DECIMAL,
                          True, commence, True))
                    inserted_closing += 2

                elif mtype == "totals":
                    for side in ["Over", "Under"]:
                        await db.execute("""
                            INSERT OR REPLACE INTO closing_lines_v2
                                (market_id, book_id, outcome_name, point,
                                 closing_price_american, closing_price_decimal,
                                 is_last_change, recorded_at, reliable)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (mid, BOOK, side, total,
                              STANDARD_AMERICAN, STANDARD_DECIMAL,
                              True, commence, True))
                    inserted_closing += 2

                elif mtype == "h2h" and ml_fav is not None:
                    fav_team = spread_fav
                    dog_team = away if spread_fav == home else home

                    await db.execute("""
                        INSERT OR REPLACE INTO closing_lines_v2
                            (market_id, book_id, outcome_name, point,
                             closing_price_american, closing_price_decimal,
                             is_last_change, recorded_at, reliable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (mid, BOOK, fav_team, None,
                          ml_fav, american_to_decimal(ml_fav),
                          True, commence, True))

                    await db.execute("""
                        INSERT OR REPLACE INTO closing_lines_v2
                            (market_id, book_id, outcome_name, point,
                             closing_price_american, closing_price_decimal,
                             is_last_change, recorded_at, reliable)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (mid, BOOK, dog_team, None,
                          ml_dog, american_to_decimal(ml_dog),
                          True, commence, True))
                    inserted_closing += 2

        await db.commit()

    print(f"\nImported {len(GAMES)} NCAAW tournament games (closing lines only):")
    print(f"  markets:         {inserted_markets} rows")
    print(f"  closing_lines:   {inserted_closing} rows")
    print(f"  game_results:    already present from ESPN (52 rows)")


if __name__ == "__main__":
    asyncio.run(import_data())
