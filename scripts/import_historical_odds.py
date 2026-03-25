"""
Bulk historical odds importer via odds-api.io Pro.

Fetches all available historical events + closing odds for target sports,
stores in historical_odds_cache with regime tags, and imports game results.

Usage:
    python scripts/import_historical_odds.py                  # All sports, full range
    python scripts/import_historical_odds.py --sport nba      # NBA only
    python scripts/import_historical_odds.py --from 2025-01-01 --to 2025-03-01
    python scripts/import_historical_odds.py --dry-run        # Show what would be imported

Coverage (odds-api.io Pro):
    NBA:  Oct 2024 → present
    NHL:  Oct 2024 → present
    NFL:  Sep 2024 → present
    MLB:  Not available (use import_historical.py for AusSportsBetting/SBR)
    NCAAB: Not available
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
SPORT_CONFIGS = {
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

SPORT_ALIASES = {"nba": "basketball_nba", "nhl": "icehockey_nhl", "nfl": "americanfootball_nfl"}

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


def normalize_event(raw: dict, sport_key: str) -> dict | None:
    """Normalize a raw odds-api.io event into Callisto format."""
    home = raw.get("home", "")
    away = raw.get("away", "")
    if not home or not away:
        return None

    bookmakers_raw = raw.get("bookmakers", {})
    if not bookmakers_raw or not isinstance(bookmakers_raw, dict):
        return None

    normalized_bookmakers = []
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

            if mkt_name == "ML":
                outcomes = []
                for o in odds_list:
                    h_dec = _safe_float(o.get("home", 0))
                    a_dec = _safe_float(o.get("away", 0))
                    if h_dec > 1 and a_dec > 1:
                        outcomes.append({"name": home, "price": _decimal_to_american(h_dec)})
                        outcomes.append({"name": away, "price": _decimal_to_american(a_dec)})
                if outcomes:
                    markets.append({"key": "h2h", "outcomes": outcomes})

            elif mkt_name == "Spread":
                best = None
                best_diff = 999
                for o in odds_list:
                    hdp = _safe_float(o.get("hdp", 0))
                    h_dec = _safe_float(o.get("home", 0))
                    a_dec = _safe_float(o.get("away", 0))
                    if h_dec > 1 and a_dec > 1:
                        diff = abs(h_dec - a_dec)
                        if diff < best_diff:
                            best_diff = diff
                            best = (hdp, h_dec, a_dec)
                if best:
                    hdp, h_dec, a_dec = best
                    markets.append({"key": "spreads", "outcomes": [
                        {"name": home, "price": _decimal_to_american(h_dec), "point": hdp},
                        {"name": away, "price": _decimal_to_american(a_dec), "point": -hdp},
                    ]})

            elif mkt_name == "Totals":
                best = None
                best_diff = 999
                for o in odds_list:
                    hdp = _safe_float(o.get("hdp", 0))
                    over_dec = _safe_float(o.get("over", 0))
                    under_dec = _safe_float(o.get("under", 0))
                    if over_dec > 1 and under_dec > 1:
                        diff = abs(over_dec - under_dec)
                        if diff < best_diff:
                            best_diff = diff
                            best = (hdp, over_dec, under_dec)
                if best:
                    hdp, over_dec, under_dec = best
                    markets.append({"key": "totals", "outcomes": [
                        {"name": "Over", "price": _decimal_to_american(over_dec), "point": hdp},
                        {"name": "Under", "price": _decimal_to_american(under_dec), "point": hdp},
                    ]})

        if markets:
            normalized_bookmakers.append({
                "key": slug,
                "title": bm_name,
                "markets": markets,
            })

    if not normalized_bookmakers:
        return None

    return {
        "id": str(raw.get("id", "")),
        "sport_key": sport_key,
        "home_team": home,
        "away_team": away,
        "commence_time": raw.get("date", ""),
        "bookmakers": normalized_bookmakers,
    }


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
    score = event.get("score", {})
    status = event.get("status", "")

    if status not in ("ended", "finished", "closed") and not score:
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
        if not uncached:
            stats["dates_skipped_cached"] += len(window_dates)
            continue

        if dry_run:
            logger.info(f"[{label}] Would fetch {w_start} → {w_end} ({len(uncached)} uncached dates)")
            stats["dates_processed"] += len(uncached)
            continue

        # Fetch events for this window
        events = await get_events(
            config["api_sport"], config["api_league"], w_start, w_end,
        )
        stats["api_requests"] += 1

        if not events:
            stats["dates_skipped_no_events"] += len(uncached)
            continue

        # Group events by date
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

                # Store game result
                store_game_result(conn, sport_key, ev, regime)
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


async def main():
    parser = argparse.ArgumentParser(description="Bulk import historical odds via odds-api.io Pro")
    parser.add_argument("--sport", type=str, help="Sport to import (nba, nhl, nfl, or all)")
    parser.add_argument("--from", dest="from_date", type=str, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--to", dest="to_date", type=str, help="End date (YYYY-MM-DD)")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be imported")
    args = parser.parse_args()

    if not API_KEY:
        logger.error("ODDS_API_IO_KEY not set in environment")
        sys.exit(1)

    # Determine sports to import
    if args.sport and args.sport != "all":
        sport_key = SPORT_ALIASES.get(args.sport.lower(), args.sport)
        if sport_key not in SPORT_CONFIGS:
            logger.error(f"Unknown sport: {args.sport}. Available: {list(SPORT_ALIASES.keys())}")
            sys.exit(1)
        sports = {sport_key: SPORT_CONFIGS[sport_key]}
    else:
        sports = SPORT_CONFIGS

    to_date = args.to_date or datetime.now().strftime("%Y-%m-%d")

    all_stats = []
    t0 = time.time()

    for sport_key, config in sports.items():
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
