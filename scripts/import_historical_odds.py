"""
Bulk historical odds importer via odds-api.io Pro.

Fetches all available historical events + closing odds for target sports,
stores in historical_odds_cache with regime tags, and imports game results.

Usage:
    python scripts/import_historical_odds.py                  # Core sports (NBA/NHL/NFL)
    python scripts/import_historical_odds.py --sport nba      # NBA only
    python scripts/import_historical_odds.py --sport mls      # MLS only
    python scripts/import_historical_odds.py --all            # All available sports
    python scripts/import_historical_odds.py --discover-golf  # Discover + import golf leagues
    python scripts/import_historical_odds.py --from 2025-01-01 --to 2025-03-01
    python scripts/import_historical_odds.py --dry-run        # Show what would be imported

Coverage (odds-api.io Pro):
    NBA:   Oct 2024 -> present
    NHL:   Oct 2024 -> present
    NFL:   Sep 2024 -> present
    MLS:   Feb 2025 -> present
    NWSL:  Mar 2025 -> present
    UFL:   Mar 2025 -> present
    Golf:  Dynamic discovery (per-tournament leagues, no persistent slug)
    MLB:   NOT AVAILABLE (zero historical events)
    NCAAB: NOT AVAILABLE
"""

import argparse
import asyncio
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env")

from tools.schema import classify_regime

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("import_historical_odds")

DB_PATH = PROJECT_ROOT / "memory" / "callisto.db"

# ── Sport configurations ──────────────────────────────────────────────
# Core sports: imported by default
CORE_SPORT_CONFIGS = {
    "basketball_nba": {
        "api_sport": "basketball",
        "api_league": "usa-nba",
        "earliest": "2024-10-01",
        "season_months": list(range(10, 13)) + list(range(1, 7)),  # Oct-Jun
        "label": "NBA",
    },
    "icehockey_nhl": {
        "api_sport": "ice-hockey",
        "api_league": "usa-nhl",
        "earliest": "2024-10-01",
        "season_months": list(range(10, 13)) + list(range(1, 7)),  # Oct-Jun
        "label": "NHL",
    },
    "americanfootball_nfl": {
        "api_sport": "american-football",
        "api_league": "usa-nfl",
        "earliest": "2024-09-01",
        "season_months": list(range(9, 13)) + [1, 2],  # Sep-Feb
        "label": "NFL",
    },
}

# Extended sports: imported with --all flag
EXTENDED_SPORT_CONFIGS = {
    "soccer_mls": {
        "api_sport": "football",
        "api_league": "usa-mls",
        "earliest": "2025-02-01",
        "season_months": list(range(2, 12)),  # Feb-Nov
        "label": "MLS",
    },
    "soccer_nwsl": {
        "api_sport": "football",
        "api_league": "usa-national-womens-soccer-league",
        "earliest": "2025-03-01",
        "season_months": list(range(3, 12)),  # Mar-Nov
        "label": "NWSL",
    },
    "americanfootball_ufl": {
        "api_sport": "american-football",
        "api_league": "usa-ufl",
        "earliest": "2025-03-01",
        "season_months": list(range(3, 8)),  # Mar-Jul
        "label": "UFL",
    },
    "soccer_usl": {
        "api_sport": "football",
        "api_league": "usa-usl-championship",
        "earliest": "2025-03-01",
        "season_months": list(range(3, 12)),  # Mar-Nov
        "label": "USL",
    },
    "icehockey_ahl": {
        "api_sport": "ice-hockey",
        "api_league": "usa-ahl",
        "earliest": "2024-10-01",
        "season_months": list(range(10, 13)) + list(range(1, 7)),  # Oct-Jun
        "label": "AHL",
    },
    "basketball_ncaam": {
        "api_sport": "basketball",
        "api_league": "usa-ncaa-division-i-national-championship",
        "earliest": "2025-03-01",
        "season_months": [3, 4],  # March Madness
        "label": "NCAAM",
    },
    "basketball_ncaaw": {
        "api_sport": "basketball",
        "api_league": "usa-ncaa-division-i-national-championship-women",
        "earliest": "2025-03-01",
        "season_months": [3, 4],  # March Madness
        "label": "NCAAW",
    },
    "baseball_mlb": {
        "api_sport": "baseball",
        "api_league": "usa-mlb",
        "earliest": "2025-03-01",
        "season_months": list(range(3, 11)),  # Mar-Oct
        "label": "MLB",
    },
    "basketball_gleague": {
        "api_sport": "basketball",
        "api_league": "usa-nba-g-league",
        "earliest": "2024-11-01",
        "season_months": list(range(11, 13)) + list(range(1, 5)),  # Nov-Apr
        "label": "G-League",
    },
}

# All configs combined
SPORT_CONFIGS = {**CORE_SPORT_CONFIGS, **EXTENDED_SPORT_CONFIGS}

SPORT_ALIASES = {
    "nba": "basketball_nba", "nhl": "icehockey_nhl", "nfl": "americanfootball_nfl",
    "mls": "soccer_mls", "nwsl": "soccer_nwsl", "ufl": "americanfootball_ufl",
    "usl": "soccer_usl", "ahl": "icehockey_ahl", "ncaam": "basketball_ncaam",
    "ncaaw": "basketball_ncaaw", "mlb": "baseball_mlb", "gleague": "basketball_gleague",
}

# ── API helpers ───────────────────────────────────────────────────────
import httpx

API_KEY = os.getenv("ODDS_API_IO_KEY", "")
API_BASE = "https://api.odds-api.io/v3"
REQUEST_DELAY = 0.12  # ~500 req/min, well under 30K/hr


async def api_get(path: str, params: dict) -> dict | list | None:
    """Make an API request with rate limiting."""
    params["apiKey"] = API_KEY
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            resp = await client.get(f"{API_BASE}{path}", params=params)
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP {e.response.status_code} for {path}: {e.response.text[:200]}")
            return None
        except Exception as e:
            logger.error(f"Request failed for {path}: {e}")
            return None


async def get_events(sport: str, league: str, from_date: str, to_date: str) -> list:
    """Get historical events for a date range (max 31 days)."""
    data = await api_get("/historical/events", {
        "sport": sport,
        "league": league,
        "from": f"{from_date}T00:00:00Z",
        "to": f"{to_date}T23:59:59Z",
    })
    await asyncio.sleep(REQUEST_DELAY)
    return data if isinstance(data, list) else []


SELECTED_BOOKMAKERS = (
    "DraftKings,Fanatics,FanDuel,BetMGM,Caesars,BetRivers,bet365 NJ,"
    "Hard Rock,Bovada,Circa,BetOnline.ag,WilliamHill NJ,"
    "Betfair Exchange,Betfair Sportsbook,Sbobet"
)


async def get_event_odds(event_id: str | int) -> dict | None:
    """Get historical/closing odds for a specific event."""
    data = await api_get("/historical/odds", {
        "eventId": str(event_id),
        "bookmakers": SELECTED_BOOKMAKERS,
    })
    await asyncio.sleep(REQUEST_DELAY)
    return data if isinstance(data, dict) else None


# ── Normalization ─────────────────────────────────────────────────────
BOOKMAKER_SLUG_MAP = {
    "DraftKings": "draftkings", "FanDuel": "fanduel", "BetMGM": "betmgm",
    "Caesars": "caesars", "BetRivers": "betrivers", "Hard Rock": "hardrock",
    "bet365 NJ": "bet365", "Bovada": "bovada", "Circa": "circa",
    "BetOnline.ag": "betonlineag", "Fanatics": "fanatics",
    "WilliamHill NJ": "williamhill", "Betfair Exchange": "betfair_exchange",
    "Betfair Sportsbook": "betfair_sportsbook", "Sbobet": "sbobet",
}


def _decimal_to_american(dec: float) -> int:
    """Convert decimal odds to American format."""
    if dec <= 1.0:
        return 100
    if dec >= 2.0:
        return round((dec - 1) * 100)
    return round(-100 / (dec - 1))


def _safe_float(val) -> float:
    try:
        return float(val)
    except (TypeError, ValueError):
        return 0.0


def _normalize_h2h(odds_list: list, home: str, away: str) -> dict | None:
    """Normalize ML / h2h market."""
    for o in odds_list:
        h_dec = _safe_float(o.get("home", 0))
        a_dec = _safe_float(o.get("away", 0))
        if h_dec > 1 and a_dec > 1:
            return {"key": "h2h", "outcomes": [
                {"name": home, "price": _decimal_to_american(h_dec)},
                {"name": away, "price": _decimal_to_american(a_dec)},
            ]}
    return None


def _normalize_spread(mkt_key: str, odds_list: list, home: str, away: str) -> dict | None:
    """Normalize any spread market — store ALL lines, not just the primary."""
    lines = []
    for o in odds_list:
        hdp = _safe_float(o.get("hdp", 0))
        h_dec = _safe_float(o.get("home", 0))
        a_dec = _safe_float(o.get("away", 0))
        if h_dec > 1 and a_dec > 1:
            lines.append({
                "point": hdp,
                "home": {"name": home, "price": _decimal_to_american(h_dec)},
                "away": {"name": away, "price": _decimal_to_american(a_dec)},
            })
    if not lines:
        return None
    # Sort by closest to even for convenience; primary line is first
    lines.sort(key=lambda x: abs(x["home"]["price"] - x["away"]["price"]))
    return {"key": mkt_key, "lines": lines}


def _normalize_total(mkt_key: str, odds_list: list) -> dict | None:
    """Normalize any totals market — store ALL lines."""
    lines = []
    for o in odds_list:
        hdp = _safe_float(o.get("hdp", 0))
        over_dec = _safe_float(o.get("over", 0))
        under_dec = _safe_float(o.get("under", 0))
        if over_dec > 1 and under_dec > 1:
            lines.append({
                "point": hdp,
                "over": _decimal_to_american(over_dec),
                "under": _decimal_to_american(under_dec),
            })
    if not lines:
        return None
    lines.sort(key=lambda x: abs(x["over"] - x["under"]))
    return {"key": mkt_key, "lines": lines}


def _normalize_yes_no(mkt_key: str, odds_list: list) -> dict | None:
    """Normalize yes/no markets (Both Teams To Score, etc.)."""
    for o in odds_list:
        y_dec = _safe_float(o.get("yes", 0))
        n_dec = _safe_float(o.get("no", 0))
        if y_dec > 1 and n_dec > 1:
            return {"key": mkt_key, "outcomes": [
                {"name": "Yes", "price": _decimal_to_american(y_dec)},
                {"name": "No", "price": _decimal_to_american(n_dec)},
            ]}
    return None


def _normalize_first_to(mkt_key: str, odds_list: list, home: str, away: str) -> dict | None:
    """Normalize first-to-score style markets."""
    for o in odds_list:
        h_dec = _safe_float(o.get("home", 0))
        a_dec = _safe_float(o.get("away", 0))
        if h_dec > 1 and a_dec > 1:
            return {"key": mkt_key, "outcomes": [
                {"name": home, "price": _decimal_to_american(h_dec)},
                {"name": away, "price": _decimal_to_american(a_dec)},
            ]}
    return None


# Map API market names to normalized keys and handler types
MARKET_HANDLERS = {
    "ML":               ("h2h",         "h2h"),
    "Spread":           ("spreads",     "spread"),
    "Spread HT":        ("spreads_ht",  "spread"),
    "Spread 1Q":        ("spreads_1q",  "spread"),
    "Spread 2Q":        ("spreads_2q",  "spread"),
    "Spread 3Q":        ("spreads_3q",  "spread"),
    "Spread 4Q":        ("spreads_4q",  "spread"),
    "Totals":           ("totals",      "total"),
    "Totals HT":        ("totals_ht",   "total"),
    "Totals 1Q":        ("totals_1q",   "total"),
    "Totals 2Q":        ("totals_2q",   "total"),
    "Totals 3Q":        ("totals_3q",   "total"),
    "Totals 4Q":        ("totals_4q",   "total"),
    "Team Total Home":  ("team_total_home", "total"),
    "Team Total Away":  ("team_total_away", "total"),
    "Both Teams To Score": ("btts",     "yesno"),
    "First Team To Score": ("first_to_score", "firstto"),
}


def normalize_event(raw: dict, sport_key: str) -> dict | None:
    """Normalize a raw odds-api.io event into Callisto format.

    Captures ALL available markets: ML, spreads, totals (full game + period),
    team totals, specials, and player props. Stores every alternate line,
    not just the primary.
    """
    home = raw.get("home", "")
    away = raw.get("away", "")
    if not home or not away:
        return None

    bookmakers_raw = raw.get("bookmakers", {})
    if not bookmakers_raw or not isinstance(bookmakers_raw, dict):
        return None

    normalized_bookmakers = []
    player_props = []

    for bm_name, bm_markets in bookmakers_raw.items():
        if not isinstance(bm_markets, list):
            continue
        slug = BOOKMAKER_SLUG_MAP.get(bm_name, bm_name.lower().replace(" ", ""))
        markets = []

        for mkt in bm_markets:
            mkt_name = mkt.get("name", "")
            odds_list = mkt.get("odds", [])
            if not odds_list:
                continue

            if mkt_name == "Player Props":
                for o in odds_list:
                    label = o.get("label", "")
                    hdp = _safe_float(o.get("hdp", 0))
                    over_dec = _safe_float(o.get("over", 0))
                    under_dec = _safe_float(o.get("under", 0))
                    if label and over_dec > 1 and under_dec > 1:
                        player_props.append({
                            "book": slug,
                            "label": label,
                            "line": hdp,
                            "over": _decimal_to_american(over_dec),
                            "under": _decimal_to_american(under_dec),
                        })
                continue

            handler = MARKET_HANDLERS.get(mkt_name)
            if not handler:
                # Unknown market — store raw for future use
                markets.append({"key": mkt_name.lower().replace(" ", "_"), "raw": odds_list})
                continue

            norm_key, handler_type = handler
            result = None
            if handler_type == "h2h":
                result = _normalize_h2h(odds_list, home, away)
            elif handler_type == "spread":
                result = _normalize_spread(norm_key, odds_list, home, away)
            elif handler_type == "total":
                result = _normalize_total(norm_key, odds_list)
            elif handler_type == "yesno":
                result = _normalize_yes_no(norm_key, odds_list)
            elif handler_type == "firstto":
                result = _normalize_first_to(norm_key, odds_list, home, away)

            if result:
                markets.append(result)

        if markets:
            normalized_bookmakers.append({
                "key": slug,
                "title": bm_name,
                "markets": markets,
            })

    if not normalized_bookmakers:
        return None

    result = {
        "id": str(raw.get("id", "")),
        "sport_key": sport_key,
        "home_team": home,
        "away_team": away,
        "commence_time": raw.get("date", ""),
        "bookmakers": normalized_bookmakers,
    }
    if player_props:
        result["player_props"] = player_props

    return result


# ── Database operations ───────────────────────────────────────────────

def get_cached_dates(conn: sqlite3.Connection, sport: str) -> set[str]:
    """Get all dates already in the historical odds cache for a sport."""
    rows = conn.execute(
        "SELECT DISTINCT snapshot_date FROM historical_odds_cache WHERE sport = ?",
        (sport,),
    ).fetchall()
    return {r[0] for r in rows}


def store_odds_cache(conn: sqlite3.Connection, sport: str, date: str, games: list, regime: str):
    """Store normalized games into historical_odds_cache."""
    cache_entry = json.dumps({
        "sport": sport,
        "date": date,
        "timestamp": f"{date}T00:00:00Z",
        "games": games,
        "game_count": len(games),
        "source": "odds_api_io_pro",
    })
    conn.execute(
        "INSERT OR REPLACE INTO historical_odds_cache "
        "(sport, snapshot_date, event_id, market_type, response_json, credits_cost, regime) "
        "VALUES (?, ?, NULL, 'h2h,spreads,totals', ?, 1, ?)",
        (sport, date, cache_entry, regime),
    )


def store_game_result(conn: sqlite3.Connection, sport: str, event: dict, regime: str):
    """Store a game result from a historical event."""
    home = event.get("home", "")
    away = event.get("away", "")
    score = event.get("scores", {}) or event.get("score", {})
    status = event.get("status", "")

    if status not in ("ended", "finished", "closed", "settled") and not score:
        return

    home_score = score.get("home")
    away_score = score.get("away")
    if home_score is None or away_score is None:
        return

    try:
        home_score = int(home_score)
        away_score = int(away_score)
    except (TypeError, ValueError):
        return

    total = home_score + away_score
    spread = home_score - away_score
    winner = home if home_score > away_score else (away if away_score > home_score else "push")

    game_date_str = event.get("date", "")[:10]

    conn.execute(
        "INSERT OR IGNORE INTO game_results "
        "(sport, game_date, home_team, away_team, home_score, away_score, "
        "total_score, spread_result, winner, source, regime) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'odds_api_io', ?)",
        (sport, game_date_str, home, away, home_score, away_score,
         total, spread, winner, regime),
    )


# ── Main import loop ──────────────────────────────────────────────────

async def import_sport(
    sport_key: str,
    config: dict,
    from_date: str,
    to_date: str,
    dry_run: bool = False,
) -> dict:
    """Import all historical data for a sport within a date range."""
    conn = sqlite3.connect(str(DB_PATH))

    # Ensure regime column exists
    try:
        conn.execute("ALTER TABLE historical_odds_cache ADD COLUMN regime TEXT")
    except Exception:
        pass
    try:
        conn.execute("ALTER TABLE game_results ADD COLUMN regime TEXT")
    except Exception:
        pass

    cached_dates = get_cached_dates(conn, sport_key)
    label = config["label"]

    stats = {
        "sport": label,
        "dates_processed": 0,
        "dates_skipped_cached": 0,
        "dates_skipped_no_events": 0,
        "events_total": 0,
        "events_with_odds": 0,
        "game_results_imported": 0,
        "api_requests": 0,
        "errors": [],
    }

    # Generate date windows (31-day max for the API)
    start = datetime.strptime(from_date, "%Y-%m-%d")
    end = datetime.strptime(to_date, "%Y-%m-%d")
    windows = []
    current = start
    while current <= end:
        window_end = min(current + timedelta(days=30), end)
        windows.append((current.strftime("%Y-%m-%d"), window_end.strftime("%Y-%m-%d")))
        current = window_end + timedelta(days=1)

    logger.info(f"[{label}] Importing {from_date} → {to_date} ({len(windows)} windows)")

    for w_start, w_end in windows:
        # Check what dates in this window are already cached
        window_dates = set()
        d = datetime.strptime(w_start, "%Y-%m-%d")
        w_end_dt = datetime.strptime(w_end, "%Y-%m-%d")
        while d <= w_end_dt:
            window_dates.add(d.strftime("%Y-%m-%d"))
            d += timedelta(days=1)

        uncached = window_dates - cached_dates

        if dry_run:
            if uncached:
                logger.info(f"[{label}] Would fetch {w_start} → {w_end} ({len(uncached)} uncached dates)")
                stats["dates_processed"] += len(uncached)
            else:
                stats["dates_skipped_cached"] += len(window_dates)
            continue

        # Fetch events for this window (always, for game results even if odds cached)
        events = await get_events(
            config["api_sport"], config["api_league"], w_start, w_end,
        )
        stats["api_requests"] += 1

        if not events:
            if not uncached:
                stats["dates_skipped_cached"] += len(window_dates)
            else:
                stats["dates_skipped_no_events"] += len(uncached)
            continue

        # Always store game results for all events
        for ev in events:
            regime = classify_regime(sport_key, ev.get("date", "")[:10] or w_start)
            store_game_result(conn, sport_key, ev, regime)

        if not uncached:
            stats["dates_skipped_cached"] += len(window_dates)
            conn.commit()
            continue

        # Group events by date for odds fetching
        events_by_date: dict[str, list] = {}
        for ev in events:
            ev_date = ev.get("date", "")[:10]
            if ev_date and ev_date in uncached:
                events_by_date.setdefault(ev_date, []).append(ev)

        # Fetch odds for each event and store
        for date_str, date_events in sorted(events_by_date.items()):
            regime = classify_regime(sport_key, date_str)
            games = []

            for ev in date_events:
                stats["events_total"] += 1
                eid = ev.get("id")
                if not eid:
                    continue

                stats["game_results_imported"] += 1

                # Fetch odds
                odds = await get_event_odds(eid)
                stats["api_requests"] += 1

                if odds and odds.get("bookmakers"):
                    normalized = normalize_event(odds, sport_key)
                    if normalized:
                        games.append(normalized)
                        stats["events_with_odds"] += 1

            if games:
                store_odds_cache(conn, sport_key, date_str, games, regime)

            stats["dates_processed"] += 1
            cached_dates.add(date_str)

        conn.commit()
        logger.info(
            f"[{label}] Window {w_start}→{w_end}: "
            f"{len(events)} events, {sum(len(v) for v in events_by_date.values())} to process"
        )

    conn.commit()
    conn.close()

    logger.info(
        f"[{label}] Done: {stats['dates_processed']} dates, "
        f"{stats['events_with_odds']}/{stats['events_total']} events with odds, "
        f"{stats['game_results_imported']} game results, "
        f"{stats['api_requests']} API requests"
    )
    return stats


# ── Golf league discovery ─────────────────────────────────────────────

GOLF_DISCOVERY_DB_PATH = PROJECT_ROOT / "memory" / "callisto.db"
GOLF_LEAGUE_TABLE = "golf_league_discovery"


def _ensure_golf_table(conn: sqlite3.Connection):
    """Create golf league discovery table if needed."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS golf_league_discovery (
            slug TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            event_count INTEGER DEFAULT 0,
            has_historical_data INTEGER DEFAULT 0,
            tournament_type TEXT DEFAULT 'matchup'
        )
    """)
    conn.commit()


async def discover_golf_leagues(dry_run: bool = False) -> list[dict]:
    """Discover all active and historical golf league slugs on odds-api.io.

    Golf on odds-api.io uses per-tournament rotating league slugs
    (e.g., 'masters-tournament-matchup', 'hero-indian-open-1st-round').
    This function catalogs them by scanning live events, then probes
    historical availability for each discovered slug.
    """
    conn = sqlite3.connect(str(GOLF_DISCOVERY_DB_PATH))
    _ensure_golf_table(conn)

    # Load previously discovered slugs
    known = {}
    for row in conn.execute("SELECT slug, name, has_historical_data FROM golf_league_discovery").fetchall():
        known[row[0]] = {"name": row[1], "has_historical": bool(row[2])}

    # Step 1: Scan live events to find current golf leagues
    logger.info("[Golf Discovery] Scanning live golf events...")
    async with httpx.AsyncClient(timeout=20.0) as client:
        params = {"apiKey": API_KEY, "sport": "golf"}
        try:
            resp = await client.get(f"{API_BASE}/events", params=params)
            resp.raise_for_status()
            events = resp.json()
        except Exception as e:
            logger.error(f"[Golf Discovery] Failed to fetch live events: {e}")
            events = []

    new_leagues = {}
    today = datetime.now().strftime("%Y-%m-%d")
    for ev in events:
        lg = ev.get("league", {})
        slug = lg.get("slug", "")
        name = lg.get("name", "")
        if slug and slug not in known:
            new_leagues[slug] = name

    if new_leagues:
        logger.info(f"[Golf Discovery] Found {len(new_leagues)} new league(s): {list(new_leagues.keys())}")
    else:
        logger.info(f"[Golf Discovery] No new leagues (tracking {len(known)} known)")

    # Step 2: Classify tournament type from slug
    def _classify_slug(slug: str) -> str:
        if "outright" in slug or "winner" in slug:
            return "outright"
        if "1st-round" in slug or "2nd-round" in slug or "3rd-round" in slug or "4th-round" in slug:
            return "round_matchup"
        if "matchup" in slug or "tournament" in slug:
            return "tournament_matchup"
        return "other"

    # Step 3: Probe historical data for new leagues
    for slug, name in new_leagues.items():
        has_historical = 0
        # Probe a few date ranges
        for probe_start, probe_end in [
            ("2025-01-01", "2025-01-31"),
            ("2025-04-01", "2025-04-30"),
            ("2025-07-01", "2025-07-31"),
            ("2025-10-01", "2025-10-31"),
        ]:
            test_events = await get_events("golf", slug, probe_start, probe_end)
            if test_events:
                has_historical = 1
                logger.info(f"[Golf Discovery] {slug}: historical data available ({len(test_events)} events in {probe_start[:7]})")
                break
            await asyncio.sleep(REQUEST_DELAY)

        ttype = _classify_slug(slug)
        conn.execute(
            "INSERT OR REPLACE INTO golf_league_discovery "
            "(slug, name, first_seen, last_seen, event_count, has_historical_data, tournament_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (slug, name, today, today, 0, has_historical, ttype),
        )

    # Update last_seen for known leagues that are still active
    for slug in known:
        if slug in {ev.get("league", {}).get("slug", "") for ev in events}:
            conn.execute("UPDATE golf_league_discovery SET last_seen = ? WHERE slug = ?", (today, slug))

    conn.commit()

    # Step 4: Import odds for all discovered leagues with events
    all_leagues = conn.execute(
        "SELECT slug, name, tournament_type FROM golf_league_discovery"
    ).fetchall()

    imported = []
    for slug, name, ttype in all_leagues:
        logger.info(f"[Golf] Checking {name} ({slug}, type={ttype})...")

        # For live leagues, import current events
        league_events = [ev for ev in events if ev.get("league", {}).get("slug") == slug]
        if not league_events:
            continue

        sport_key = f"golf_{slug.replace('-', '_')}"
        regime = "current"
        games = []
        api_requests = 0

        for ev in league_events:
            eid = ev.get("id")
            if not eid:
                continue

            store_game_result(conn, "golf_pga", ev, regime)

            if not dry_run:
                odds = await get_event_odds(eid)
                api_requests += 1
                if odds and odds.get("bookmakers"):
                    normalized = normalize_event(odds, "golf_pga")
                    if normalized:
                        games.append(normalized)

        ev_date = today
        if games:
            store_odds_cache(conn, "golf_pga", ev_date, games, regime)
            conn.commit()

        imported.append({
            "league": name,
            "slug": slug,
            "type": ttype,
            "events": len(league_events),
            "with_odds": len(games),
            "api_requests": api_requests,
        })
        logger.info(f"[Golf] {name}: {len(league_events)} events, {len(games)} with odds")

    conn.close()

    # Summary
    print("\n" + "=" * 60)
    print("GOLF LEAGUE DISCOVERY SUMMARY")
    print("=" * 60)
    print(f"  Total leagues tracked: {len(all_leagues)}")
    print(f"  New leagues found:     {len(new_leagues)}")
    for imp in imported:
        print(f"  {imp['league']}:")
        print(f"    Type: {imp['type']}, Events: {imp['events']}, With odds: {imp['with_odds']}")
    print("=" * 60)

    return imported


async def main():
    parser = argparse.ArgumentParser(description="Bulk import historical odds via odds-api.io Pro")
    parser.add_argument("--sport", type=str, help="Sport to import (nba, nhl, nfl, mls, nwsl, ufl, usl, ahl)")
    parser.add_argument("--all", action="store_true", help="Import all available sports (core + extended)")
    parser.add_argument("--from", dest="from_date", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--discover-golf", action="store_true", help="Discover and import golf leagues")
    parser.add_argument("--recent", action="store_true",
                        help="Only import last 90 days (odds retention window)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    args = parser.parse_args()

    if not API_KEY:
        logger.error("ODDS_API_IO_KEY not set in environment")
        sys.exit(1)

    # Golf discovery mode
    if args.discover_golf:
        await discover_golf_leagues(args.dry_run)
        return

    # Determine sports to import
    if args.sport:
        sport_key = SPORT_ALIASES.get(args.sport.lower(), args.sport)
        if sport_key not in SPORT_CONFIGS:
            logger.error(f"Unknown sport: {args.sport}. Available: {list(SPORT_ALIASES.keys())}")
            sys.exit(1)
        sports = {sport_key: SPORT_CONFIGS[sport_key]}
    elif args.all:
        sports = SPORT_CONFIGS
    else:
        # Default: core sports only
        sports = CORE_SPORT_CONFIGS

    to_date = args.to_date or datetime.now().strftime("%Y-%m-%d")

    all_stats = []
    t0 = time.time()

    for sport_key, config in sports.items():
        if args.recent:
            from_date = (datetime.now() - timedelta(days=90)).strftime("%Y-%m-%d")
        else:
            from_date = args.from_date or config["earliest"]
        stats = await import_sport(sport_key, config, from_date, to_date, args.dry_run)
        all_stats.append(stats)

    elapsed = time.time() - t0

    # Summary
    print("\n" + "=" * 60)
    print("IMPORT SUMMARY")
    print("=" * 60)
    total_requests = 0
    for s in all_stats:
        print(f"\n  {s['sport']}:")
        print(f"    Dates processed:     {s['dates_processed']}")
        print(f"    Dates skipped (cache):{s['dates_skipped_cached']}")
        print(f"    Events with odds:    {s['events_with_odds']}/{s['events_total']}")
        print(f"    Game results:        {s['game_results_imported']}")
        print(f"    API requests:        {s['api_requests']}")
        total_requests += s["api_requests"]
    print(f"\n  Total API requests: {total_requests}")
    print(f"  Elapsed: {elapsed:.0f}s")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
