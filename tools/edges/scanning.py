"""Cross-book / alt-line / sharp-money scanning helpers (split from edge_scanner)."""

from __future__ import annotations

import logging
from typing import Optional

from tools.odds_api import calculate_implied_probability, calculate_ev, find_best_line
from tools.book_keys import canonicalize_book

from tools.edge_scanner import SOFT_TITLES
from tools.edges.common import (
    logger,
    _DEBUG_WEIGHTS,
    _DEAD_NUM_SPORT_ALIASES,
    _filter_in_progress_games,
    get_sharp_titles_for_sport,
    weighted_sharp_consensus,
    _scan_line_group,
)

def scan_cross_book_edges(games: list[dict], market: str = "spreads", sport: str = "") -> list[dict]:
    """
    Scan all games for cross-bookmaker pricing divergence.

    Returns edges sorted by magnitude. A large spread across books on the
    same line means at least one book is mispriced — the question is which one.

    Sharp books (Pinnacle, Circa, Bookmaker.eu) set the true line.
    Soft books (FanDuel, DraftKings, BetMGM) lag behind and offer value.

    When Granger temporal prediction data is available for the sport,
    the identified leader book is dynamically added to the sharp set.
    """
    games = _filter_in_progress_games(games)

    # Dynamic sharp set — Granger leader (if available) enriches the static set
    SHARP_TITLES = get_sharp_titles_for_sport(sport)

    edges = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for team in [home, away]:
            if not team:
                continue

            best = find_best_line(game, market=market, team=team)
            if best.get("error") or len(best.get("all_lines", [])) < 2:
                continue

            all_lines = best["all_lines"]

            # SPREAD POINT VALIDATION: For spreads/totals, lines with
            # different point values are different bets and must NOT be
            # averaged together. Previously we dropped the minority point
            # value silently — that killed key-number arbitrage. Now we
            # build one line-group per point value and process each group
            # through the full edge-scoring path so e.g. "DK is on +2.5,
            # everyone else is on +3" surfaces as its own candidate.
            if market in ("spreads", "totals"):
                from collections import Counter, defaultdict
                point_counts = Counter(l.get("point") for l in all_lines)
                if len(point_counts) > 1:
                    grouped = defaultdict(list)
                    for l in all_lines:
                        grouped[l.get("point")].append(l)
                    _line_groups = [grp for grp in grouped.values() if len(grp) >= 2]
                    if not _line_groups:
                        continue
                    if len(_line_groups) > 1:
                        logger.info(
                            f"Point split for {team} {market}: "
                            f"points={dict(point_counts)} → {len(_line_groups)} sub-edges"
                        )
                else:
                    _line_groups = [all_lines]
            else:
                _line_groups = [all_lines]

            for _group in _line_groups:
                _scan_line_group(
                    edges=edges,
                    lines=_group,
                    game=game,
                    home=home,
                    away=away,
                    team=team,
                    market=market,
                    sport=sport,
                    SHARP_TITLES=SHARP_TITLES,
                    SOFT_TITLES=SOFT_TITLES,
                )

    # Sort by implied range descending — biggest disagreements first
    edges.sort(key=lambda x: x["implied_range"], reverse=True)
    return edges

# ---------------------------------------------------------------------------
# Alt-line edge scanning
# ---------------------------------------------------------------------------
#
# Every alternate spread / total / prop line is its own market — a -3.5 alt
# spread has a different win probability from the -2.5 main line, and books
# price them independently. Historically we only scanned the main line, which
# left "key number arbitrage" on the table: when one book sits on -3 (a dead
# number in NFL) while another offers -3.5 (a key number), the -3.5 line is
# mathematically superior by ~2% and can go uncontested for minutes.
#
# Design:
#   - fetch_alt_lines_for_games(games, sport): per-event odds-api call for
#     alternate_spreads + alternate_totals, cached 15 min per event_id to
#     keep credit burn bounded (~2 events × 1 call per 15 min = ~4/hr/sport).
#   - scan_alt_line_edges(games_with_alts, sport): runs the normal
#     cross-book scanner once per alt point value and tags results as
#     "alt_line" so they're distinguishable from main-line edges.
#
# The cache is process-local — production callers reuse the same scanner
# instance across snapshots so the 15-min TTL is enough to prevent
# duplicate calls on the same slate.

import time as _alt_time

_ALT_LINE_CACHE: dict[str, tuple[float, dict]] = {}
_ALT_LINE_TTL_S = 15 * 60  # 15 minutes


def _alt_cache_get(key: str) -> Optional[dict]:
    entry = _ALT_LINE_CACHE.get(key)
    if not entry:
        return None
    ts, data = entry
    if _alt_time.time() - ts > _ALT_LINE_TTL_S:
        _ALT_LINE_CACHE.pop(key, None)
        return None
    return data


def _alt_cache_put(key: str, data: dict) -> None:
    _ALT_LINE_CACHE[key] = (_alt_time.time(), data)
    # Simple LRU-ish cap: drop oldest when >500 entries. Each entry is small.
    if len(_ALT_LINE_CACHE) > 500:
        oldest_key = min(_ALT_LINE_CACHE, key=lambda k: _ALT_LINE_CACHE[k][0])
        _ALT_LINE_CACHE.pop(oldest_key, None)


async def fetch_alt_lines_for_games(games: list[dict], sport: str) -> list[dict]:
    """Fetch alternate spreads / totals for each upcoming game, with per-event
    caching. Returns the games list with an extra ``alt_bookmakers`` key on
    each game holding the alternate-line bookmaker array from odds-api.

    Low-credit-burn: at most 1 odds-api call per event per 15 minutes. Call
    this before ``scan_alt_line_edges`` in the main loop. Games already in
    progress are skipped (the pre-game filter runs inside).
    """
    from tools.odds_api import get_alternate_lines

    pre_game = _filter_in_progress_games(games)
    enriched = []
    for g in pre_game:
        eid = g.get("id", "")
        if not eid:
            enriched.append(g)
            continue
        cache_key = f"{sport}:{eid}"
        cached = _alt_cache_get(cache_key)
        if cached is not None:
            g2 = dict(g)
            g2["alt_bookmakers"] = cached.get("bookmakers", [])
            enriched.append(g2)
            continue
        try:
            resp = await get_alternate_lines(sport, eid)
            if resp.get("error"):
                logger.debug(f"alt lines fetch error {sport}/{eid}: {resp['error']}")
                enriched.append(g)
                continue
            _alt_cache_put(cache_key, resp)
            g2 = dict(g)
            g2["alt_bookmakers"] = resp.get("bookmakers", [])
            enriched.append(g2)
        except Exception as e:
            logger.debug(f"alt lines fetch exception {sport}/{eid}: {e}")
            enriched.append(g)
    return enriched


def scan_alt_line_edges(games: list[dict], sport: str = "") -> list[dict]:
    """Scan alternate-line markets for cross-book divergence.

    Expects each game dict to carry an ``alt_bookmakers`` key populated by
    ``fetch_alt_lines_for_games``. For each sport-relevant alt market
    (alternate_spreads, alternate_totals, plus prop alt-lines when present),
    groups outcomes by point value and feeds every point group through the
    standard cross-book scanner so each alt point becomes its own candidate
    edge — producing the "key number arbitrage" signal the April audit
    flagged as a dead zone.

    Returns the same edge dict shape as ``scan_cross_book_edges`` with an
    additional ``is_alt_line": True`` marker and ``alt_market`` name.
    """
    games = _filter_in_progress_games(games)

    SHARP_TITLES = get_sharp_titles_for_sport(sport)

    edges: list[dict] = []

    for game in games:
        alt_bookmakers = game.get("alt_bookmakers") or []
        if not alt_bookmakers:
            continue
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        # Flatten to (market_key, team, point) -> [line dicts]
        from collections import defaultdict
        grouped: dict[tuple[str, str, float], list[dict]] = defaultdict(list)
        for bm in alt_bookmakers:
            bm_title = bm.get("title") or bm.get("key", "")
            fetched_at = bm.get("last_update")
            for mkt in bm.get("markets", []):
                mkey = mkt.get("key", "")
                if not mkey:
                    continue
                # Only scan alt markets plus alt prop markets
                if not (mkey.startswith("alternate_") or mkey.startswith("player_")):
                    continue
                for outcome in mkt.get("outcomes", []):
                    team_or_side = outcome.get("name", "")
                    point = outcome.get("point")
                    price = outcome.get("price")
                    if price is None or point is None or not team_or_side:
                        continue
                    grouped[(mkey, team_or_side, float(point))].append({
                        "bookmaker": bm_title,
                        "price": int(price) if isinstance(price, (int, float)) else price,
                        "point": float(point),
                        "last_update": fetched_at,
                    })

        # Run each (market, side, point) group through the line-group scanner.
        for (mkey, side, point), lines in grouped.items():
            if len(lines) < 2:
                continue
            # Map alt market back to the base market for downstream tooling.
            if mkey == "alternate_spreads":
                base_market = "spreads"
            elif mkey == "alternate_totals":
                base_market = "totals"
            else:
                base_market = mkey  # player props keep their market key
            _scan_line_group(
                edges=edges,
                lines=lines,
                game=game,
                home=home,
                away=away,
                team=side,
                market=base_market,
                sport=sport,
                SHARP_TITLES=SHARP_TITLES,
                SOFT_TITLES=SOFT_TITLES,
            )
            # Tag the last-added edge (if one was produced) as an alt line.
            if edges and edges[-1].get("game_id", "") == game.get("id", ""):
                edges[-1]["is_alt_line"] = True
                edges[-1]["alt_market"] = mkey
                edges[-1]["alt_point"] = point

    edges.sort(key=lambda x: x["implied_range"], reverse=True)
    return edges

def detect_sharp_money(old_snapshot: dict, new_snapshot: dict) -> list[dict]:
    """
    Detect sharp money by finding games where ONE book moved but others didn't.

    When Pinnacle or a sharp book moves a line and the retail books haven't
    followed yet, there's a window. Sharp money caused the move — the retail
    books WILL follow, it's just a matter of when.

    This is the "steam move" concept:
    1. Sharp bettor places large wager on a soft book
    2. That book adjusts its line
    3. Other books haven't received the same action yet
    4. Window exists to bet the OLD line at other books before they adjust
    """
    old_prices = {}  # (game_id, book, market, team) -> price
    new_prices = {}

    for snapshot, store in [(old_snapshot, old_prices), (new_snapshot, new_prices)]:
        for game in snapshot.get("games", []):
            gid = game.get("id", "")
            for bm in game.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    for outcome in mkt.get("outcomes", []):
                        key = (gid, bm["key"], mkt["key"], outcome.get("name", ""))
                        store[key] = {
                            "price": outcome.get("price", 0),
                            "point": outcome.get("point"),
                            "bookmaker": bm["title"],
                        }

    # Group by (game_id, market, team) to compare across books
    from collections import defaultdict
    game_lines = defaultdict(list)

    for key in new_prices:
        gid, book_key, market, team = key
        group_key = (gid, market, team)
        old = old_prices.get(key)
        new = new_prices[key]
        if old:
            price_diff = new["price"] - old["price"]
            game_lines[group_key].append({
                "bookmaker": new["bookmaker"],
                "book_key": book_key,
                "old_price": old["price"],
                "new_price": new["price"],
                "price_diff": price_diff,
                "old_point": old.get("point"),
                "new_point": new.get("point"),
                "point_diff": (new.get("point") or 0) - (old.get("point") or 0),
            })

    sharp_signals = []
    for (gid, market, team), books in game_lines.items():
        if len(books) < 3:
            continue

        # Count how many books moved significantly
        movers = [b for b in books if abs(b["price_diff"]) >= 8 or abs(b["point_diff"]) >= 0.5]
        stale = [b for b in books if abs(b["price_diff"]) < 3 and abs(b["point_diff"]) < 0.5]

        # Sharp signal: 1-2 books moved, majority didn't
        if 0 < len(movers) <= 2 and len(stale) >= 2:
            sharp_signals.append({
                "game_id": gid,
                "market": market,
                "team": team,
                "moved_books": [{
                    "bookmaker": m["bookmaker"],
                    "old_price": m["old_price"],
                    "new_price": m["new_price"],
                    "movement": m["price_diff"],
                } for m in movers],
                "stale_books": [{
                    "bookmaker": s["bookmaker"],
                    "price": s["new_price"],
                    "point": s.get("new_point"),
                } for s in stale],
                "signal": "SHARP_MOVE",
                "interpretation": (
                    f"{len(movers)} book(s) moved on {team} {market} while "
                    f"{len(stale)} book(s) haven't adjusted. "
                    f"Stale books may offer value before they follow."
                ),
            })

    return sharp_signals
