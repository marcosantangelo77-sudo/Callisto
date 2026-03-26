"""
Edge scanner — finds exploitable inefficiencies across bookmakers.

This is the quantitative core. Three edge types:

1. CROSS-BOOK DIVERGENCE: Same bet priced differently across books.
   If BetMGM has -105 and MyBookie has -125 on the same spread,
   BetMGM is giving you 4%+ better implied probability. Sharp money
   moves first on soft books — divergence tells you WHERE sharps are.

2. SHARP MONEY DETECTION: When one book moves while others don't,
   that book likely took a large sharp bet. The others will follow.
   Getting in before the cascade = buying at a discount.

3. MISPRICED LINES: When a book's juice structure creates +EV.
   Example: if both sides of a spread are -105 instead of -110,
   the total vig is lower and the line may be exploitable.
   Also: stale lines that haven't adjusted to news/injuries.
"""

import logging
from typing import Optional

from tools.odds_api import (
    calculate_implied_probability,
    calculate_ev,
    find_best_line,
)
from tools.market_microstructure import compute_market_metrics

logger = logging.getLogger("callisto.edge_scanner")

# Hardcoded fallback — always used when Granger data is unavailable
_STATIC_SHARP_TITLES = {"pinnacle", "lowvig.ag", "bookmaker.eu", "betonline.ag", "betcris", "circa", "betfair exchange", "betfair", "sbobet"}

# Cache for Granger-derived sharp leader per sport (sport -> (leader, timestamp))
_granger_sharp_cache: dict[str, tuple[str, float]] = {}
_GRANGER_CACHE_TTL = 3600  # 1 hour — re-query DB at most once per hour


def get_sharp_titles_for_sport(sport: str = "") -> set[str]:
    """Return the set of sharp book titles, enriched by Granger leadership data.

    If Granger temporal prediction analysis has identified a leader for this
    sport, that book is added to the sharp set. Falls back to the static
    hardcoded set when no Granger data exists.

    This is a sync function safe for the hot path — it reads from a cache
    populated by the async Granger phase in the research loop.
    """
    import time
    sharp = set(_STATIC_SHARP_TITLES)

    if not sport:
        return sharp

    cached = _granger_sharp_cache.get(sport)
    if cached:
        leader, ts = cached
        if time.time() - ts < _GRANGER_CACHE_TTL and leader:
            sharp.add(leader)
            return sharp

    # Try async lookup — but only if we're inside a running event loop
    # If not, just return the static set (the cache will be populated by
    # the research loop's Granger phase)
    try:
        import asyncio
        import os
        loop = asyncio.get_running_loop()
        # We're in an async context — schedule cache refresh but don't block
        db_path = os.getenv("CALLISTO_DB_PATH", "memory/callisto.db")
        loop.create_task(_refresh_granger_cache(sport, db_path))
    except RuntimeError:
        pass  # No running loop — return static set

    return sharp


async def _refresh_granger_cache(sport: str, db_path: str) -> None:
    """Refresh the Granger sharp leader cache for a sport."""
    import time
    try:
        from tools.granger_causality import get_sharp_leader
        leader = await get_sharp_leader(db_path, sport)
        _granger_sharp_cache[sport] = (leader, time.time())
        if leader:
            logger.info(f"Granger sharp leader for {sport}: {leader}")
    except Exception as e:
        logger.debug(f"Granger cache refresh failed for {sport}: {e}")


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
    # Dynamic sharp set — Granger leader (if available) enriches the static set
    SHARP_TITLES = get_sharp_titles_for_sport(sport)
    SOFT_TITLES = {"fanduel", "draftkings", "betmgm", "pointsbet", "caesars", "betrivers", "mybookie.ag", "bovada", "betus", "fanatics", "fanatics sportsbook"}

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

            # SPREAD POINT VALIDATION: For spreads/totals, only compare lines
            # with the same point value.  Mixing e.g. +1.5 and -1.5 (or alt
            # spreads like +2.5) produces phantom 20-30% "edges" that are
            # actually two completely different bets being compared.
            if market in ("spreads", "totals"):
                from collections import Counter
                point_counts = Counter(l.get("point") for l in all_lines)
                if len(point_counts) > 1:
                    # Keep only lines matching the most common point value
                    dominant_point = point_counts.most_common(1)[0][0]
                    mismatched = [l for l in all_lines if l.get("point") != dominant_point]
                    if mismatched:
                        logger.warning(
                            f"Point mismatch for {team} {market}: points={dict(point_counts)}, "
                            f"keeping only point={dominant_point}"
                        )
                    all_lines = [l for l in all_lines if l.get("point") == dominant_point]
                    if len(all_lines) < 2:
                        continue

            best_line = max(all_lines, key=lambda x: x["price"])
            worst_line = min(all_lines, key=lambda x: x["price"])
            price_spread = best_line["price"] - worst_line["price"]

            # H2H sanity check: if lines contain both large positive and large
            # negative prices, both sides of the market leaked into one team's
            # line set (e.g. favorite -750 mixed with opponent's underdog +610).
            # This produces phantom edges of 50%+ that are physically impossible.
            if market == "h2h":
                prices = [l["price"] for l in all_lines]
                has_big_pos = any(p > 150 for p in prices)
                has_big_neg = any(p < -150 for p in prices)
                if has_big_pos and has_big_neg:
                    logger.warning(
                        f"H2H line contamination for {team}: prices span "
                        f"{min(prices)} to {max(prices)} — skipping"
                    )
                    continue

            # Calculate implied probability range across books
            implied_probs = [calculate_implied_probability(l["price"]) for l in all_lines]
            implied_range = max(implied_probs) - min(implied_probs)
            avg_implied = sum(implied_probs) / len(implied_probs)

            # Sanity: implied range > 25% is almost certainly data contamination
            if implied_range > 0.25:
                logger.warning(
                    f"Implausible implied range {implied_range:.1%} for {team} "
                    f"{market} — likely data contamination, skipping"
                )
                continue

            # Classify which books are sharp vs soft for this line
            sharp_lines = [l for l in all_lines if l["bookmaker"].lower() in SHARP_TITLES]
            soft_lines = [l for l in all_lines if l["bookmaker"].lower() in SOFT_TITLES]

            # Sharp consensus = average of sharp book implied probabilities
            sharp_consensus = None
            if sharp_lines:
                sharp_implied = [calculate_implied_probability(l["price"]) for l in sharp_lines]
                sharp_consensus = sum(sharp_implied) / len(sharp_implied)

            # Edge: soft book offers better price than sharp consensus
            soft_edges = []
            if sharp_consensus is not None:
                for sl in soft_lines:
                    soft_implied = calculate_implied_probability(sl["price"])
                    # If soft book implies LOWER probability than sharps think,
                    # the soft book is underpricing this outcome = value
                    edge = sharp_consensus - soft_implied
                    # Cap: real edges in efficient markets top out ~15%.
                    # Anything higher is almost certainly a data/calc bug.
                    if edge > 0.20:
                        logger.warning(
                            f"Implausible edge {edge:.1%} for {team} at "
                            f"{sl['bookmaker']} — likely data contamination"
                        )
                        continue
                    if edge > 0.02:  # 2% minimum edge
                        ev = calculate_ev(
                            probability=sharp_consensus,
                            american_odds=sl["price"],
                        )
                        soft_edges.append({
                            "bookmaker": sl["bookmaker"],
                            "price": sl["price"],
                            "point": sl.get("point"),
                            "edge_vs_sharp": round(edge, 4),
                            "ev": ev,
                        })

            if price_spread >= 10 or implied_range >= 0.03:
                # Compute market microstructure metrics
                book_name_list = [l["bookmaker"] for l in all_lines]
                micro = compute_market_metrics(implied_probs, book_name_list, SHARP_TITLES)

                edges.append({
                    "game": f"{away} @ {home}",
                    "game_id": game.get("id", ""),
                    "team": team,
                    "market": market,
                    "best_line": {
                        "bookmaker": best_line["bookmaker"],
                        "price": best_line["price"],
                        "point": best_line.get("point"),
                    },
                    "worst_line": {
                        "bookmaker": worst_line["bookmaker"],
                        "price": worst_line["price"],
                        "point": worst_line.get("point"),
                    },
                    "price_spread": price_spread,
                    "implied_range": round(implied_range, 4),
                    "avg_implied": round(avg_implied, 4),
                    "sharp_consensus": round(sharp_consensus, 4) if sharp_consensus else None,
                    "num_bookmakers": len(all_lines),
                    "soft_book_edges": soft_edges,
                    "book_count": len(all_lines),
                    "hhi": micro["hhi_overall"],
                    "entropy": micro["entropy_overall"],
                })

    # Sort by implied range descending — biggest disagreements first
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


def scan_vig_edges(games: list[dict], market: str = "spreads") -> list[dict]:
    """
    Find books offering unusually low vig (juice) on specific games.

    Standard vig: both sides at -110 = 4.55% total vig.
    Low vig: -105/-105 = 2.44% total vig.
    Reduced vig = the book is either promoting or mispricing.

    Books with lower vig give you better prices structurally —
    over thousands of bets, reduced vig is the simplest edge.
    """
    vig_edges = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != market:
                    continue

                outcomes = mkt.get("outcomes", [])
                if len(outcomes) < 2:
                    continue

                # Calculate total implied probability (vig = total - 1.0)
                total_implied = sum(
                    calculate_implied_probability(o.get("price", -110))
                    for o in outcomes
                )
                vig = total_implied - 1.0

                # Standard vig on spreads is ~4.5% (-110/-110)
                # Anything under 3% is notable, under 2% is exceptional
                if vig < 0.035:
                    vig_edges.append({
                        "game": f"{away} @ {home}",
                        "game_id": game.get("id", ""),
                        "bookmaker": bm["title"],
                        "market": market,
                        "vig_pct": round(vig * 100, 2),
                        "total_implied": round(total_implied, 4),
                        "outcomes": [
                            {
                                "name": o.get("name", ""),
                                "price": o.get("price", 0),
                                "point": o.get("point"),
                                "implied": round(calculate_implied_probability(o.get("price", -110)), 4),
                            }
                            for o in outcomes
                        ],
                        "edge_type": "LOW_VIG",
                        "note": (
                            f"Vig at {round(vig * 100, 1)}% vs standard ~4.5%. "
                            f"{'Exceptional value' if vig < 0.02 else 'Notable reduction'}."
                        ),
                    })

    vig_edges.sort(key=lambda x: x["vig_pct"])
    return vig_edges


def full_edge_scan(snapshot: dict) -> dict:
    """
    Run all edge scanners on a snapshot and return a unified report.

    This is the main entry point — call after each odds snapshot.
    """
    games = snapshot.get("games", [])
    if not games:
        return {"error": "No games in snapshot", "edges": []}

    report = {
        "game_count": len(games),
        "sport": snapshot.get("sport", "unknown"),
    }

    # Cross-book divergence
    sport = snapshot.get("sport", "")
    for market in ["spreads", "h2h", "totals"]:
        key = f"cross_book_{market}"
        edges = scan_cross_book_edges(games, market=market, sport=sport)
        report[key] = edges
        if edges:
            logger.info(
                f"Cross-book {market}: {len(edges)} divergences found, "
                f"max implied range: {edges[0]['implied_range']:.1%}"
            )

    # Vig analysis
    for market in ["spreads", "h2h", "totals"]:
        key = f"low_vig_{market}"
        vig = scan_vig_edges(games, market=market)
        report[key] = vig
        if vig:
            logger.info(f"Low vig {market}: {len(vig)} edges, lowest: {vig[0]['vig_pct']}%")

    # Summary
    total_edges = sum(
        len(report.get(k, []))
        for k in report
        if k.startswith("cross_book_") or k.startswith("low_vig_")
    )
    report["total_edges"] = total_edges

    return report
