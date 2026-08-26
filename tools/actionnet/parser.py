"""Parsing of Action Network scoreboard responses."""

import logging
from datetime import datetime, timezone
from typing import Optional

from tools.actionnet.constants import (
    _API_BASE,
    _BOOK_IDS,
    BOOK_ID_MAP,
    LEAGUE_MAP,
    SPORT_TITLES,
)
from tools.actionnet.team_names import _resolve_team_name

logger = logging.getLogger("callisto.actionnet.parser")


def build_url(sport: str, date_str: Optional[str] = None) -> str:
    """Build the Action Network scoreboard API URL."""
    league = LEAGUE_MAP.get(sport)
    if not league:
        raise ValueError(f"Unsupported sport: {sport}. Supported: {list(LEAGUE_MAP.keys())}")
    if date_str is None:
        date_str = datetime.now(timezone.utc).strftime("%Y%m%d")
    return f"{_API_BASE}/{league}?period=game&bookIds={_BOOK_IDS}&date={date_str}"


def parse_game(game_data: dict, sport: str) -> Optional[dict]:
    """
    Parse a single game from the Action Network response into
    the standard Callisto odds format.
    """
    # Extract teams
    teams = game_data.get("teams", [])
    if len(teams) < 2:
        return None

    home_team_raw = None
    away_team_raw = None
    for team in teams:
        # Prefer full_name (e.g. "Charlotte Hornets") over display_name ("Hornets")
        name = team.get("full_name") or team.get("display_name", "")
        if team.get("is_home") is True:
            home_team_raw = name
        elif team.get("is_away") is True:
            away_team_raw = name

    # Fallback: if is_home/is_away not set, use position.
    # Action Network API returns teams[0]=HOME, teams[1]=AWAY.
    # ml_home/ml_away in odds correspond to teams[0]/teams[1] respectively.
    if home_team_raw is None or away_team_raw is None:
        if len(teams) >= 2:
            t0 = teams[0].get("full_name") or teams[0].get("display_name", "Unknown")
            t1 = teams[1].get("full_name") or teams[1].get("display_name", "Unknown")
            home_team_raw = home_team_raw or t0
            away_team_raw = away_team_raw or t1
        else:
            return None

    # If full_name was available, use it directly; otherwise resolve short name
    home_team = home_team_raw if " " in home_team_raw else _resolve_team_name(home_team_raw, sport)
    away_team = away_team_raw if " " in away_team_raw else _resolve_team_name(away_team_raw, sport)

    # Commence time
    start_time = game_data.get("start_time", "")
    if start_time:
        # Action Network returns ISO format — normalize to Z suffix
        if not start_time.endswith("Z") and "+" not in start_time:
            start_time = start_time + "Z"
    else:
        start_time = datetime.now(timezone.utc).isoformat()

    # Game ID
    game_id = game_data.get("id", "")
    normalized_id = f"action_{game_id}" if game_id else f"action_{home_team}_{away_team}"

    # Parse odds from each bookmaker
    odds_list = game_data.get("odds", [])
    bookmakers = []

    for odds_entry in odds_list:
        book_id = odds_entry.get("book_id")
        book_info = BOOK_ID_MAP.get(book_id)
        if not book_info:
            continue  # Unknown book — skip

        book_key, book_title = book_info
        markets = []

        # Moneyline (h2h)
        ml_home = odds_entry.get("ml_home")
        ml_away = odds_entry.get("ml_away")
        if ml_home is not None and ml_away is not None:
            try:
                markets.append({
                    "key": "h2h",
                    "outcomes": [
                        {"name": home_team, "price": int(ml_home)},
                        {"name": away_team, "price": int(ml_away)},
                    ],
                })
            except (ValueError, TypeError):
                pass

        # Spreads
        spread_home = odds_entry.get("spread_home")
        spread_away = odds_entry.get("spread_away")
        spread_home_line = odds_entry.get("spread_home_line")
        spread_away_line = odds_entry.get("spread_away_line")
        if all(v is not None for v in [spread_home, spread_away, spread_home_line, spread_away_line]):
            try:
                markets.append({
                    "key": "spreads",
                    "outcomes": [
                        {"name": home_team, "price": int(spread_home_line), "point": float(spread_home)},
                        {"name": away_team, "price": int(spread_away_line), "point": float(spread_away)},
                    ],
                })
            except (ValueError, TypeError):
                pass

        # Totals
        total = odds_entry.get("total")
        over = odds_entry.get("over")
        under = odds_entry.get("under")
        if all(v is not None for v in [total, over, under]):
            try:
                markets.append({
                    "key": "totals",
                    "outcomes": [
                        {"name": "Over", "price": int(over), "point": float(total)},
                        {"name": "Under", "price": int(under), "point": float(total)},
                    ],
                })
            except (ValueError, TypeError):
                pass

        if markets:
            bookmakers.append({
                "key": book_key,
                "title": book_title,
                "last_update": datetime.now(timezone.utc).isoformat(),
                "markets": markets,
            })

    if not bookmakers:
        return None

    return {
        "id": normalized_id,
        "sport_key": sport,
        "sport_title": SPORT_TITLES.get(sport, sport),
        "home_team": home_team,
        "away_team": away_team,
        "commence_time": start_time,
        "bookmakers": bookmakers,
    }


def extract_public_betting(game_data: dict, sport: str) -> Optional[dict]:
    """Extract public betting percentages from a single game's odds entries."""
    teams = game_data.get("teams", [])
    if len(teams) < 2:
        return None

    home_team_raw = None
    away_team_raw = None
    for team in teams:
        name = team.get("full_name") or team.get("display_name", "")
        if team.get("is_home") is True:
            home_team_raw = name
        elif team.get("is_away") is True:
            away_team_raw = name

    if not home_team_raw or not away_team_raw:
        if len(teams) >= 2:
            t0 = teams[0].get("full_name") or teams[0].get("display_name", "Unknown")
            t1 = teams[1].get("full_name") or teams[1].get("display_name", "Unknown")
            home_team_raw = home_team_raw or t0
            away_team_raw = away_team_raw or t1
        else:
            return None

    home_team = _resolve_team_name(home_team_raw, sport)
    away_team = _resolve_team_name(away_team_raw, sport)

    # Collect public betting data from all books that report it
    public_entries = []
    for odds_entry in game_data.get("odds", []):
        ml_home_public = odds_entry.get("ml_home_public")
        ml_away_public = odds_entry.get("ml_away_public")
        spread_home_public = odds_entry.get("spread_home_public")
        spread_away_public = odds_entry.get("spread_away_public")
        total_over_public = odds_entry.get("total_over_public")
        total_under_public = odds_entry.get("total_under_public")

        book_id = odds_entry.get("book_id")
        book_info = BOOK_ID_MAP.get(book_id, (str(book_id), str(book_id)))

        entry = {"book_key": book_info[0], "book_title": book_info[1]}
        has_data = False

        if ml_home_public is not None and ml_away_public is not None:
            entry["ml_home_pct"] = ml_home_public
            entry["ml_away_pct"] = ml_away_public
            has_data = True

        if spread_home_public is not None and spread_away_public is not None:
            entry["spread_home_pct"] = spread_home_public
            entry["spread_away_pct"] = spread_away_public
            has_data = True

        if total_over_public is not None and total_under_public is not None:
            entry["total_over_pct"] = total_over_public
            entry["total_under_pct"] = total_under_public
            has_data = True

        if has_data:
            public_entries.append(entry)

    if not public_entries:
        return None

    # Compute average public percentages across all books that report them
    avg = {}
    for field_pair in [("ml_home_pct", "ml_away_pct"),
                       ("spread_home_pct", "spread_away_pct"),
                       ("total_over_pct", "total_under_pct")]:
        vals_a = [e[field_pair[0]] for e in public_entries if field_pair[0] in e]
        vals_b = [e[field_pair[1]] for e in public_entries if field_pair[1] in e]
        if vals_a and vals_b:
            avg[field_pair[0]] = round(sum(vals_a) / len(vals_a), 1)
            avg[field_pair[1]] = round(sum(vals_b) / len(vals_b), 1)

    return {
        "home_team": home_team,
        "away_team": away_team,
        "game_id": game_data.get("id", ""),
        "start_time": game_data.get("start_time", ""),
        "averages": avg,
        "by_book": public_entries,
    }
