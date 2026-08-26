"""
Fallback odds-source cascade for the line monitor.

Extracted from tools/line_monitor.py. When Odds API credits are low or the
API key is missing, LineMonitor collects snapshots from free sources:

  1. Odds-API.io Pro (PRIMARY — 15 books, 30K req/hr)
  2. DraftKings scraper (supplementary — DK-specific lines)
  3. Action Network scraper (supplementary — up to 9 books)
  4. FanDuel scraper (supplementary)
  4b. Fanatics scraper (secondary book; sport-gated by FANATICS_LEAGUE_KEYS)

BetMGM is DISABLED (redundant with odds-api.io Pro) and OddsPapi was REMOVED
2026-04-18 — do not reintroduce without an explicit decision.

collect_free_sources returns {source_name: data} for every source that
succeeded; merge_free_sources folds them into a single multi-book snapshot
via tools.lines.ingest.merge_free_snapshots.
"""

import logging

from tools.dk_scraper import scrape_dk_odds
from tools.action_network_scraper import scrape_action_network
from tools.fanduel_scraper import scrape_fd_odds
from tools.fanatics_scraper import fetch_fanatics_odds

from tools.lines.ingest import merge_free_snapshots

logger = logging.getLogger("callisto.line_monitor.snapshot_ops")


async def collect_free_sources(sport: str, *, odds_api_io_get_odds, odds_api_io_usage) -> dict:
    """Run the fallback cascade and return {source_name: snapshot_data}.

    Each scraper is isolated in its own try/except: one failing source never
    blocks the others.
    """
    scraped = {}  # source_name -> data

    # 1. Odds-API.io Pro — PRIMARY source (15 books, 30K req/hr)
    try:
        usage = odds_api_io_usage()
        if usage.get("requests_remaining_this_hour", usage.get("requests_remaining", 0)) > 0 and usage.get("api_key_set"):
            io_data = await odds_api_io_get_odds(sport)
            if not io_data.get("error") and io_data.get("game_count", 0) > 0:
                scraped["odds_api_io"] = io_data
                logger.info(f"Odds-API.io Pro {sport}: {io_data['game_count']} games ({len(io_data['games'][0]['bookmakers']) if io_data['games'] else 0} books/game)")
    except Exception as e:
        logger.warning(f"Odds-API.io Pro failed for {sport}: {e}")

    # 2. DraftKings — free, supplementary for DK-specific alt lines
    try:
        dk_data = await scrape_dk_odds(sport)
        if not dk_data.get("error") and dk_data.get("game_count", 0) > 0:
            scraped["dk"] = dk_data
    except Exception as e:
        logger.warning(f"DK scraper failed for {sport}: {e}")

    # 3. Action Network — free, up to 9 books per game
    try:
        an_data = await scrape_action_network(sport)
        if not an_data.get("error") and an_data.get("game_count", 0) > 0:
            scraped["action_network"] = an_data
    except Exception as e:
        logger.warning(f"Action Network scraper failed for {sport}: {e}")

    # 4. FanDuel — free and unlimited
    try:
        fd_data = await scrape_fd_odds(sport)
        if not fd_data.get("error") and fd_data.get("game_count", 0) > 0:
            scraped["fd"] = fd_data
    except Exception as e:
        logger.warning(f"FanDuel scraper failed for {sport}: {e}")

    # 4b. Fanatics — secondary book per project_sportsbooks. Free public
    # endpoint; cookie-optional (CALLISTO_FANATICS_SESSION_COOKIE upgrades
    # to authed reads). Skip sports Fanatics doesn't carry (golf, MLS).
    try:
        from tools.fanatics_scraper import FANATICS_LEAGUE_KEYS
        if sport in FANATICS_LEAGUE_KEYS:
            fan_data = await fetch_fanatics_odds(sport)
            if not fan_data.get("error") and fan_data.get("game_count", 0) > 0:
                scraped["fanatics"] = fan_data
    except Exception as e:
        logger.warning(f"Fanatics scraper failed for {sport}: {e}")

    return scraped


def merge_free_sources(scraped: dict, sport: str) -> dict:
    """Merge all successful source payloads into one multi-book snapshot."""
    sources = list(scraped.values())
    merged = sources[0]
    for extra in sources[1:]:
        merged = merge_free_snapshots(merged, extra)

    merged["source"] = f"free_cascade_{'_'.join(scraped.keys())}"
    logger.info(
        f"Fallback snapshot {sport}: merged {list(scraped.keys())} = "
        f"{merged.get('game_count', 0)} games"
    )
    return merged
