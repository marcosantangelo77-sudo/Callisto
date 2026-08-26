"""Normalization: odds-api.io format -> Callisto standard format.

Split out of tools/odds_api_io.py — see tools/odds_io package docstring.
"""

from datetime import datetime, timezone
from typing import Optional

from tools.odds_io.config import BOOKMAKER_SLUG_MAP, SPORT_TITLES


def decimal_to_american(dec: float) -> int:
    """Convert decimal odds to American format."""
    if dec >= 2.0:
        return round((dec - 1) * 100)
    elif dec > 1.0:
        return round(-100 / (dec - 1))
    return -10000


def safe_float(val) -> Optional[float]:
    """Safely convert a value to float."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def normalize_event_odds(raw: dict, event_info: dict, sport: str) -> Optional[dict]:
    """
    Normalize a single event's odds response from odds-api.io to the
    standard Callisto format.

    odds-api.io response structure:
    {
        "id": 62924773,
        "home": "Phoenix Suns",
        "away": "Denver Nuggets",
        "date": "2026-03-25T03:00:00Z",
        "status": "pending",
        "bookmakers": {
            "BetMGM": [
                {"name": "ML", "updatedAt": "...", "odds": [{"home": "2.95", "away": "1.43"}]},
                {"name": "Spread", "updatedAt": "...", "odds": [{"hdp": 6.5, "home": "1.91", "away": "1.91"}]},
                {"name": "Totals", "updatedAt": "...", "odds": [{"hdp": 226.5, "over": "1.87", "under": "1.95"}]}
            ]
        }
    }
    """
    if not raw or not isinstance(raw, dict):
        return None

    home_team = raw.get("home", event_info.get("home_team", ""))
    away_team = raw.get("away", event_info.get("away_team", ""))
    commence_time = raw.get("date", event_info.get("commence_time", ""))

    game = {
        "id": str(raw.get("id", event_info.get("id", ""))),
        "sport_key": sport,
        "sport_title": SPORT_TITLES.get(sport, sport),
        "home_team": home_team,
        "away_team": away_team,
        "commence_time": commence_time,
        "bookmakers": [],
    }

    raw_bookmakers = raw.get("bookmakers", {})
    if not isinstance(raw_bookmakers, dict):
        return None

    for bm_name, bm_markets in raw_bookmakers.items():
        bm_slug = BOOKMAKER_SLUG_MAP.get(bm_name, bm_name.lower().replace(" ", "_"))
        normalized_markets = []
        last_update = ""

        if not isinstance(bm_markets, list):
            continue

        for mkt in bm_markets:
            mkt_name = mkt.get("name", "").lower().strip()
            updated_at = mkt.get("updatedAt", "")
            if updated_at:
                last_update = updated_at

            odds_list = mkt.get("odds", [])
            if not odds_list:
                continue

            # Classify market type
            if mkt_name in ("ml", "moneyline", "1x2", "winner"):
                # Moneyline — pick the primary line (first entry)
                odds_entry = odds_list[0]
                home_dec = safe_float(odds_entry.get("home"))
                away_dec = safe_float(odds_entry.get("away"))
                if home_dec and away_dec:
                    normalized_markets.append({
                        "key": "h2h",
                        "last_update": updated_at,
                        "outcomes": [
                            {"name": home_team, "price": decimal_to_american(home_dec)},
                            {"name": away_team, "price": decimal_to_american(away_dec)},
                        ],
                    })

            elif mkt_name in ("spread", "spreads", "handicap", "point spread"):
                # Spreads — find the primary spread (closest to the main line)
                # Pick the entry with the tightest odds or the middle index
                best = pick_primary_spread(odds_list, home_team, away_team)
                if best:
                    normalized_markets.append({
                        "key": "spreads",
                        "last_update": updated_at,
                        "outcomes": best,
                    })

            elif mkt_name in ("totals", "total", "over/under", "total points"):
                # Totals — find the primary total
                best = pick_primary_total(odds_list)
                if best:
                    normalized_markets.append({
                        "key": "totals",
                        "last_update": updated_at,
                        "outcomes": best,
                    })

        if normalized_markets:
            game["bookmakers"].append({
                "key": bm_slug,
                "title": bm_name,
                "last_update": last_update,
                "markets": normalized_markets,
            })

    if not game["bookmakers"]:
        return None

    return game


def pick_primary_spread(odds_list: list, home: str, away: str) -> Optional[list]:
    """
    From a list of spread entries, pick the primary (main) spread.
    The primary spread is typically the one closest to -110/-110 (even odds).
    """
    best_entry = None
    best_score = float("inf")

    for entry in odds_list:
        hdp = entry.get("hdp")
        home_dec = safe_float(entry.get("home"))
        away_dec = safe_float(entry.get("away"))
        if hdp is None or not home_dec or not away_dec:
            continue
        # Score: how close to even (1.91 is -110 in decimal)
        score = abs(home_dec - 1.91) + abs(away_dec - 1.91)
        if score < best_score:
            best_score = score
            best_entry = entry

    if not best_entry:
        return None

    hdp = float(best_entry["hdp"])
    home_dec = safe_float(best_entry["home"])
    away_dec = safe_float(best_entry["away"])

    return [
        {"name": home, "price": decimal_to_american(home_dec), "point": hdp},
        {"name": away, "price": decimal_to_american(away_dec), "point": -hdp},
    ]


def pick_primary_total(odds_list: list) -> Optional[list]:
    """
    From a list of total entries, pick the primary (main) total.
    Same logic: closest to -110/-110.
    """
    best_entry = None
    best_score = float("inf")

    for entry in odds_list:
        hdp = entry.get("hdp")
        over_dec = safe_float(entry.get("over"))
        under_dec = safe_float(entry.get("under"))
        if hdp is None or not over_dec or not under_dec:
            continue
        score = abs(over_dec - 1.91) + abs(under_dec - 1.91)
        if score < best_score:
            best_score = score
            best_entry = entry

    if not best_entry:
        return None

    hdp = float(best_entry["hdp"])
    over_dec = safe_float(best_entry["over"])
    under_dec = safe_float(best_entry["under"])

    return [
        {"name": "Over", "price": decimal_to_american(over_dec), "point": hdp},
        {"name": "Under", "price": decimal_to_american(under_dec), "point": hdp},
    ]


# ---------------------------------------------------------------------------
# Pre-commence snapshot helpers — lookahead-free backtesting
# ---------------------------------------------------------------------------


def parse_iso(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp. Returns None if unparseable."""
    if not ts:
        return None
    try:
        s = ts.replace("Z", "+00:00") if ts.endswith("Z") else ts
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def extract_movement_snapshots(movements_raw: dict | list) -> list[dict]:
    """Normalize odds-movements response into a list of
    {time: datetime, odds: {...}} entries, sorted ascending by time.

    The odds-api.io movements endpoint returns varying shapes depending on
    market. Accept whatever it gives us and extract every entry that has a
    parseable timestamp + odds payload.
    """
    raw = movements_raw
    if isinstance(raw, dict):
        # Common shapes: {"movements": [...]}, {"history": [...]}, {"data": [...]}
        for key in ("movements", "history", "data", "items", "odds"):
            if isinstance(raw.get(key), list):
                raw = raw[key]
                break
    if not isinstance(raw, list):
        return []

    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        ts_str = (
            entry.get("time") or entry.get("updatedAt")
            or entry.get("timestamp") or entry.get("date")
        )
        dt = parse_iso(ts_str) if isinstance(ts_str, str) else None
        if dt is None:
            continue
        out.append({"time": dt, "raw": entry})
    out.sort(key=lambda x: x["time"])
    return out


def snapshot_to_market_outcomes(
    entry_raw: dict, market: str, home: str, away: str,
) -> Optional[dict]:
    """Turn a single movement entry into a normalized market dict (same shape
    as normalize_event_odds() market output)."""
    market_lc = market.lower().strip()

    # Movement entries use the same per-market schema as historical/odds:
    #   ML:      {"home": "2.95", "away": "1.43"}
    #   Spread:  {"hdp": -6.5, "home": "1.91", "away": "1.91"}
    #   Totals:  {"hdp": 226.5, "over": "1.87", "under": "1.95"}
    odds = entry_raw.get("odds") or entry_raw
    if isinstance(odds, list) and odds:
        odds = odds[0]
    if not isinstance(odds, dict):
        return None

    if market_lc in ("ml", "moneyline", "h2h"):
        home_dec = safe_float(odds.get("home"))
        away_dec = safe_float(odds.get("away"))
        if not home_dec or not away_dec:
            return None
        return {
            "key": "h2h",
            "last_update": "",
            "outcomes": [
                {"name": home, "price": decimal_to_american(home_dec)},
                {"name": away, "price": decimal_to_american(away_dec)},
            ],
        }
    if market_lc in ("spread", "spreads", "handicap"):
        hdp = odds.get("hdp")
        home_dec = safe_float(odds.get("home"))
        away_dec = safe_float(odds.get("away"))
        if hdp is None or not home_dec or not away_dec:
            return None
        hdp_f = float(hdp)
        return {
            "key": "spreads",
            "last_update": "",
            "outcomes": [
                {"name": home, "price": decimal_to_american(home_dec), "point": hdp_f},
                {"name": away, "price": decimal_to_american(away_dec), "point": -hdp_f},
            ],
        }
    if market_lc in ("totals", "total", "over/under"):
        hdp = odds.get("hdp")
        over_dec = safe_float(odds.get("over"))
        under_dec = safe_float(odds.get("under"))
        if hdp is None or not over_dec or not under_dec:
            return None
        hdp_f = float(hdp)
        return {
            "key": "totals",
            "last_update": "",
            "outcomes": [
                {"name": "Over", "price": decimal_to_american(over_dec), "point": hdp_f},
                {"name": "Under", "price": decimal_to_american(under_dec), "point": hdp_f},
            ],
        }
    return None


def pick_pre_commence_entry(
    entries: list[dict], commence: Optional[datetime], lead_minutes: int,
) -> Optional[dict]:
    """Return the LATEST entry with time <= commence - lead_minutes.
    None if no such entry exists (fallback will be triggered)."""
    if not entries or commence is None:
        return None
    cutoff = commence.timestamp() - (lead_minutes * 60)
    candidates = [e for e in entries if e["time"].timestamp() <= cutoff]
    if not candidates:
        return None
    return candidates[-1]  # latest by time (entries are sorted ascending)


# ---------------------------------------------------------------------------
# Utility: compare with other sources
# ---------------------------------------------------------------------------

def find_best_line(game: dict, market: str = "spreads", team: str = "") -> dict:
    """
    Compare lines across bookmakers for a game and find the best available.

    Same interface as odds_api.find_best_line() — works with any game dict
    in the standard format regardless of source.
    """
    bookmaker_lines = []
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            if mkt["key"] != market:
                continue
            for outcome in mkt.get("outcomes", []):
                entry = {
                    "bookmaker": bm.get("title", bm.get("key", "")),
                    "name": outcome.get("name", ""),
                    "price": outcome.get("price", 0),
                    "point": outcome.get("point"),
                    "last_update": bm.get("last_update", ""),
                }
                if not team or team.lower() in outcome.get("name", "").lower():
                    bookmaker_lines.append(entry)

    if not bookmaker_lines:
        return {"error": "No lines found", "lines": []}

    bookmaker_lines.sort(key=lambda x: x["price"], reverse=True)

    return {
        "best": bookmaker_lines[0],
        "worst": bookmaker_lines[-1],
        "spread_across_books": bookmaker_lines[0]["price"] - bookmaker_lines[-1]["price"],
        "all_lines": bookmaker_lines,
    }
