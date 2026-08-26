"""Snapshot ingest helpers for the line monitor.

Pure(ish) functions extracted from tools/line_monitor.py:
- odds-api.io WS/incremental message → snapshot conversion
- WS sport-key mapping
- delta-into-snapshot merging (multi-book consensus preservation)
- fetched_at stamping
- scraper enrichment (DK / FD / BetMGM / Fanatics share one pattern)
- free-source snapshot merging + matchup keys

LineMonitor delegates here; the import path tools.line_monitor.LineMonitor
is unchanged.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("callisto.lines.ingest")

# Map odds-api.io WS sport slugs back to the-odds-api.com sport keys used in
# odds_snapshots rows. A single WS sport fans out to multiple leagues.
WS_SPORT_TO_MONITORED: dict[str, list[str]] = {
    "basketball": ["basketball_nba", "basketball_ncaab", "basketball_ncaaw"],
    "american-football": ["americanfootball_nfl", "americanfootball_ncaaf"],
    "baseball": ["baseball_mlb"],
    "ice-hockey": ["icehockey_nhl"],
    "soccer": ["soccer_mls", "soccer_epl"],
}


def ws_sport_to_monitored(ws_sport: str, ws_league: str = "") -> Optional[str]:
    """Map odds-api.io WS (sport, league) to the-odds-api.com sport key.

    WS messages carry e.g. sport='basketball', league='NBA'. We convert
    that back to 'basketball_nba' so every downstream consumer (edge
    scanner, movement detector, odds_snapshots rows) sees the same
    canonical sport key regardless of whether the event arrived by WS,
    incremental poll, or 15-min snapshot.
    """
    s = (ws_sport or "").lower().strip()
    lg = (ws_league or "").lower().strip().replace(" ", "_")
    # Preferred: combine sport + league so basketball_ncaab and
    # basketball_nba don't collide in edge-scan output.
    if s == "basketball":
        if "ncaa" in lg and "w" in lg:
            return "basketball_ncaaw"
        if "ncaa" in lg:
            return "basketball_ncaab"
        return "basketball_nba"
    if s in ("american-football", "american_football", "football"):
        if "ncaa" in lg:
            return "americanfootball_ncaaf"
        return "americanfootball_nfl"
    if s == "baseball":
        return "baseball_mlb"
    if s in ("ice-hockey", "ice_hockey", "hockey"):
        return "icehockey_nhl"
    if s == "soccer":
        return "soccer_mls"
    # Last resort — first matching entry in WS_SPORT_TO_MONITORED.
    first = WS_SPORT_TO_MONITORED.get(s, [])
    if first:
        return first[0]
    return None


def canonicalize_book_top(name: str) -> str:
    """Thin wrapper — imports lazily to avoid circular imports at module load."""
    try:
        from tools.book_keys import canonicalize_book as _cb
        return _cb(name)
    except Exception:
        return (name or "").lower().replace(" ", "_")


# odds-api.io WS market names → the-odds-api.com market keys.
_WS_MARKET_MAP = {
    "ml": "h2h", "moneyline": "h2h",
    "spread": "spreads", "spreads": "spreads", "runline": "spreads",
    "totals": "totals", "total": "totals", "ou": "totals",
}


def ws_update_to_snapshot(data: dict) -> Optional[tuple[str, dict]]:
    """Convert a single odds-api.io WS/incremental message into a snapshot.

    Returns (sport_key, snapshot_dict) or None if the message lacks enough
    structure to route. The snapshot dict is shaped like get_odds() output
    so the process-snapshot pipeline can consume it unchanged — one game,
    one bookmaker, and the subset of markets that actually changed.
    """
    if not isinstance(data, dict):
        return None
    event_id = data.get("id") or data.get("event_id")
    if not event_id:
        return None
    ws_sport = data.get("sport", "") or data.get("sport_key", "")
    ws_league = data.get("league", "")
    sport_key = ws_sport_to_monitored(str(ws_sport), str(ws_league))
    if not sport_key:
        return None

    bookie_name = data.get("bookie") or data.get("bookmaker") or ""
    if not bookie_name:
        return None

    now_iso = datetime.now(timezone.utc).isoformat()
    # Build an odds-api-shaped bookmaker entry. odds-api.io WS markets look
    # like {"name": "ML"|"Spread"|"Totals", "outcomes": [{"name", "price",
    # "point"?}]} — map onto the-odds-api.com's "key" vocabulary.
    bm_markets = []
    for m in data.get("markets", []) or []:
        raw = str(m.get("name", "")).lower()
        key = _WS_MARKET_MAP.get(raw, raw)
        outcomes = []
        for oc in m.get("outcomes", []) or []:
            outcomes.append({
                "name": oc.get("name", ""),
                "price": oc.get("price", 0),
                "point": oc.get("point"),
                "fetched_at": now_iso,
            })
        if outcomes:
            bm_markets.append({"key": key, "outcomes": outcomes})
    if not bm_markets:
        return None

    snapshot = {
        "sport": sport_key,
        "game_count": 1,
        "source": "odds_api_io",
        "fetched_at": now_iso,
        "games": [{
            "id": str(event_id),
            "sport_key": sport_key,
            "home_team": data.get("home", "") or data.get("home_team", ""),
            "away_team": data.get("away", "") or data.get("away_team", ""),
            "commence_time": data.get("commence") or data.get("commence_time"),
            "bookmakers": [{
                "key": canonicalize_book_top(bookie_name),
                "title": bookie_name,
                "last_update": now_iso,
                "fetched_at": now_iso,
                "markets": bm_markets,
            }],
        }],
    }
    return sport_key, snapshot


def merge_delta_into_snapshot(base: dict, delta: dict, now_iso: str) -> dict:
    """Splice a single-book WS/incremental delta onto the full snapshot.

    Keeps every game + book from `base`, then for each game in `delta`
    replaces OR appends the matching bookmaker entry. The returned dict is
    a shallow copy — callers may mutate per-game entries in place.

    Crucially, this preserves multi-book consensus: when DK pushes a WS
    update, the returned snapshot still has every other book's quote from
    the last 15-min snapshot (aged but weighted-down via fetched_at decay
    in edge_scanner), and DK's entry is replaced with the fresh quote.
    """
    import copy
    merged = {
        "sport": base.get("sport", delta.get("sport", "")),
        "game_count": base.get("game_count", 0),
        "source": delta.get("source", base.get("source", "odds_api")),
        "fetched_at": now_iso,
        "ingest_source": delta.get("ingest_source", "ws"),
        "games": [copy.deepcopy(g) for g in base.get("games", [])],
    }
    # Index base games by id for O(1) splice.
    by_id: dict[str, dict] = {}
    for g in merged["games"]:
        gid = str(g.get("id", ""))
        if gid:
            by_id[gid] = g

    for dgame in delta.get("games", []) or []:
        gid = str(dgame.get("id", ""))
        if not gid or gid not in by_id:
            # New event that hasn't been seen in base — append wholesale.
            merged["games"].append(copy.deepcopy(dgame))
            continue
        target = by_id[gid]
        target.setdefault("bookmakers", [])
        existing = target["bookmakers"]
        for dbm in dgame.get("bookmakers", []) or []:
            dkey = (dbm.get("key") or "").lower()
            dtitle = (dbm.get("title") or "").lower()
            replaced = False
            for i, bm in enumerate(existing):
                bmkey = (bm.get("key") or "").lower()
                bmtitle = (bm.get("title") or "").lower()
                if dkey and bmkey == dkey:
                    existing[i] = copy.deepcopy(dbm)
                    replaced = True
                    break
                if dtitle and bmtitle == dtitle:
                    existing[i] = copy.deepcopy(dbm)
                    replaced = True
                    break
            if not replaced:
                existing.append(copy.deepcopy(dbm))
    merged["game_count"] = len(merged["games"])
    return merged


def stamp_snapshot_fetched_at(snapshot: dict, now_iso: str) -> None:
    """Stamp `fetched_at` on every bookmaker entry in a snapshot.

    Prefers an existing `fetched_at` (so WS-delivered deltas retain their
    true ingest time even when later merged into a 15-min snapshot frame)
    and falls back to `last_update` → `now_iso` otherwise. The outermost
    snapshot dict also receives `fetched_at` so per-provider tooling
    (scraper fallback, incremental poll) can pass the stamp through without
    digging into every bookmaker.
    """
    snapshot.setdefault("fetched_at", now_iso)
    for game in snapshot.get("games", []) or []:
        for bm in game.get("bookmakers", []) or []:
            # Bookmaker-level fetched_at — don't overwrite WS stamps
            if not bm.get("fetched_at"):
                bm["fetched_at"] = bm.get("last_update") or now_iso
            # Outcome-level for granular freshness (WS messages deliver a
            # single outcome change; stamp that outcome)
            for mkt in bm.get("markets", []) or []:
                for oc in mkt.get("outcomes", []) or []:
                    if not oc.get("fetched_at"):
                        oc["fetched_at"] = bm.get("fetched_at", now_iso)


def matchup_key(home: str, away: str) -> str:
    """Normalize team names into a matchup key for cross-source matching."""
    if not home or not away:
        return ""
    # Lowercase, strip common suffixes, sort for consistency
    h = home.lower().strip()
    a = away.lower().strip()
    return f"{min(a, h)}|{max(a, h)}"


async def enrich_with_scraper(
    sport: str,
    snapshot: dict,
    scrape_fn,
    book_key: str,
    key_variants: tuple[str, ...] = (),
) -> dict:
    """Merge fresh scraper data for one book into an odds snapshot.

    For each game in the snapshot, if the scraper has data for the same
    matchup, update (or add) that book's bookmaker entry with the fresher
    scraped lines. Shared implementation behind the per-book enrichment
    helpers (DK / FanDuel / BetMGM / Fanatics).
    """
    try:
        data = await scrape_fn(sport)
        if data.get("error") or not data.get("games"):
            return snapshot

        by_matchup: dict[str, dict] = {}
        for scraped_game in data["games"]:
            key = matchup_key(scraped_game.get("home_team", ""), scraped_game.get("away_team", ""))
            if key:
                by_matchup[key] = scraped_game

        enriched = 0
        variants = {book_key} | {v.lower() for v in key_variants}
        for game in snapshot.get("games", []):
            key = matchup_key(game.get("home_team", ""), game.get("away_team", ""))
            if not key or key not in by_matchup:
                continue

            scraped_game = by_matchup[key]
            bookmaker = None
            for bm in scraped_game.get("bookmakers", []):
                if bm.get("key") == book_key:
                    bookmaker = bm
                    break

            if not bookmaker:
                continue

            # Find and replace existing entry (including spelling
            # variants), or append if absent.
            replaced = False
            for i, bm in enumerate(game.get("bookmakers", [])):
                if bm.get("key", "").lower() in variants:
                    game["bookmakers"][i] = bookmaker
                    replaced = True
                    break

            if not replaced:
                game.setdefault("bookmakers", []).append(bookmaker)

            enriched += 1

        if enriched > 0:
            logger.info(
                f"{book_key} enrichment {sport}: updated "
                f"{enriched}/{len(snapshot.get('games', []))} games"
            )

    except Exception as e:
        logger.warning(f"{book_key} enrichment failed for {sport}: {e}", exc_info=True)

    return snapshot


def merge_free_snapshots(base_data: dict, extra_data: dict) -> dict:
    """Merge two odds snapshots into one multi-book snapshot.

    Uses base_data as the foundation, then adds bookmaker entries from
    extra_data to matching games. Extra-only games are appended.
    Works with any pair of sources (DK+FD, DK+MGM, etc.).
    """
    merged = {
        "sport": base_data.get("sport", extra_data.get("sport", "")),
        "games": [dict(g) for g in base_data.get("games", [])],
        "source": "merged",
        "credits": {"remaining": None, "used": None, "api_key_set": True},
    }

    # Build matchup lookup from base games
    base_by_matchup = {}
    for i, game in enumerate(merged["games"]):
        key = matchup_key(game.get("home_team", ""), game.get("away_team", ""))
        if key:
            base_by_matchup[key] = i

    extra_only_games = []
    for extra_game in extra_data.get("games", []):
        key = matchup_key(extra_game.get("home_team", ""), extra_game.get("away_team", ""))
        if key and key in base_by_matchup:
            idx = base_by_matchup[key]
            # Add bookmakers from extra source, skipping duplicates.
            # A duplicate = same bookmaker key already present in base.
            existing_keys = {
                bm.get("key", "").lower()
                for bm in merged["games"][idx].get("bookmakers", [])
            }
            for bm in extra_game.get("bookmakers", []):
                bm_key = bm.get("key", "").lower()
                if bm_key and bm_key in existing_keys:
                    continue  # Skip — this book already has an entry
                merged["games"][idx].setdefault("bookmakers", []).append(bm)
                if bm_key:
                    existing_keys.add(bm_key)
        else:
            extra_only_games.append(extra_game)

    merged["games"].extend(extra_only_games)
    merged["game_count"] = len(merged["games"])

    # Enforce sport_key on all games to prevent cross-sport contamination
    sport_key = merged["sport"]
    if sport_key:
        for g in merged["games"]:
            if not g.get("sport_key"):
                g["sport_key"] = sport_key

    return merged
