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
from tools.dead_numbers import (
    is_dead_number as _is_dead_number,
    key_number_value as _key_number_value,
    find_dead_number_steals,
    analyze_spread as _analyze_spread,
    rank_line_shopping_opportunities,
    buy_points_analysis,
    SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES,
)

logger = logging.getLogger("callisto.edge_scanner")

# ---------------------------------------------------------------------------
# Sport key -> pace_model.Sport mapping for API sport keys
# ---------------------------------------------------------------------------
_PACE_SPORT_MAP = {
    "basketball_nba": "nba",
    "americanfootball_nfl": "nfl",
    "baseball_mlb": "mlb",
    "icehockey_nhl": "nhl",
    "soccer_epl": "soccer",
    "soccer_germany_bundesliga": "soccer",
    "soccer_spain_la_liga": "soccer",
    "soccer_italy_serie_a": "soccer",
    "soccer_france_ligue_one": "soccer",
    "soccer_usa_mls": "soccer",
}

# Low-scoring sports that should use Poisson distribution for totals
_LOW_SCORING_SPORTS = {"mlb", "nhl", "soccer"}

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

                # Dead number / key number enrichment for spreads/totals
                dead_num_info = {}
                if market in ("spreads", "totals") and sport:
                    _dn_sport = sport.lower()
                    if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
                        best_point = best_line.get("point")
                        if best_point is not None:
                            try:
                                dead_num_info["is_dead_number"] = _is_dead_number(best_point, _dn_sport)
                                dead_num_info["key_number_importance"] = _key_number_value(best_point, _dn_sport)
                            except (ValueError, KeyError):
                                pass  # Unsupported sport — skip silently

                # Line shopping analysis: compare best vs worst spread across books
                line_shopping_info = {}
                if market == "spreads" and sport:
                    _dn_sport = sport.lower()
                    if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
                        best_pt = best_line.get("point")
                        worst_pt = worst_line.get("point")
                        if best_pt is not None and worst_pt is not None and best_pt != worst_pt:
                            try:
                                from tools.dead_numbers import line_shopping_value
                                lsv = line_shopping_value(best_pt, worst_pt, _dn_sport)
                                line_shopping_info = {
                                    "prob_difference_pct": lsv.get("prob_difference_pct", 0),
                                    "cents_value": lsv.get("cents_value", 0),
                                    "crossed_key_numbers": lsv.get("crossed_key_numbers", []),
                                    "recommendation": lsv.get("recommendation", ""),
                                }
                            except (ValueError, KeyError):
                                pass

                # Compute no-vig fair probabilities and market hold using math_utils
                fair_probs = None
                market_hold = None
                try:
                    from tools.math_utils import no_vig_price as _nvp, calculate_hold as _ch, american_to_decimal as _atd
                    if len(all_lines) >= 2:
                        # Use best and worst to compute no-vig price range
                        fair_probs = _nvp(best_line["price"], worst_line["price"])
                        dec_odds = [_atd(l["price"]) for l in all_lines[:2]]
                        market_hold = round(_ch(dec_odds), 4)
                except Exception:
                    pass

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
                    "no_vig_fair_probs": [round(p, 4) for p in fair_probs] if fair_probs else None,
                    "market_hold": market_hold,
                    "num_bookmakers": len(all_lines),
                    "soft_book_edges": soft_edges,
                    "book_count": len(all_lines),
                    "hhi": micro["hhi_overall"],
                    "entropy": micro["entropy_overall"],
                    **dead_num_info,
                    "line_shopping": line_shopping_info if line_shopping_info else None,
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

                # Use math_utils for overround and hold calculations
                try:
                    from tools.math_utils import calculate_overround, calculate_hold, american_to_decimal
                    decimal_odds = [american_to_decimal(o.get("price", -110)) for o in outcomes]
                    vig = calculate_overround(decimal_odds)
                    hold = calculate_hold(decimal_odds)
                except ImportError:
                    # Fallback: inline calculation
                    total_implied = sum(
                        calculate_implied_probability(o.get("price", -110))
                        for o in outcomes
                    )
                    vig = total_implied - 1.0
                    hold = vig / (1.0 + vig) if (1.0 + vig) > 0 else 0

                # Standard vig on spreads is ~4.5% (-110/-110)
                # Anything under 3% is notable, under 2% is exceptional
                if vig < 0.035:
                    vig_edges.append({
                        "game": f"{away} @ {home}",
                        "game_id": game.get("id", ""),
                        "bookmaker": bm["title"],
                        "market": market,
                        "vig_pct": round(vig * 100, 2),
                        "hold_pct": round(hold * 100, 2),
                        "total_implied": round(1.0 + vig, 4),
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
                            f"Vig at {round(vig * 100, 1)}% (hold {round(hold * 100, 1)}%) vs standard ~4.5%. "
                            f"{'Exceptional value' if vig < 0.02 else 'Notable reduction'}."
                        ),
                    })

    vig_edges.sort(key=lambda x: x["vig_pct"])
    return vig_edges


def scan_pace_model_total_edges(
    games: list[dict],
    sport: str,
    weather_data: Optional[dict] = None,
    venue_team: Optional[str] = None,
    refs: Optional[list[str]] = None,
) -> list[dict]:
    """
    Scan games for total (over/under) edges using the pace model + environment.

    This provides an INDEPENDENT total estimate beyond cross-book divergence.
    The pace model projects totals from first principles (pace x efficiency),
    then the environment module adjusts for weather/venue/refs.

    The result supplements — does not replace — cross-book edge detection.

    Args:
        games: List of game dicts from odds snapshot.
        sport: Odds API sport key (e.g. 'basketball_nba').
        weather_data: Optional weather dict for outdoor games.
        venue_team: Home team abbreviation for venue lookup.
        refs: Optional referee names for the game.

    Returns:
        List of model-based total edge dicts.
    """
    pace_sport = _PACE_SPORT_MAP.get(sport.lower())
    if not pace_sport:
        return []

    try:
        from tools.pace_model import (
            project_game_total,
            detect_total_edge,
            poisson_total_distribution,
            LEAGUE_DEFAULTS,
            Sport,
        )
        from tools.environment import total_environment_adjustment
    except ImportError as e:
        logger.debug(f"Pace/environment import failed: {e}")
        return []

    sport_enum = Sport(pace_sport)
    defaults = LEAGUE_DEFAULTS.get(sport_enum, {})
    if not defaults:
        return []

    # Get league average values for this sport
    if sport_enum == Sport.NBA:
        league_avg_pace = defaults["pace"]
        league_avg_eff = defaults["off_eff"]
    elif sport_enum == Sport.NFL:
        league_avg_pace = defaults["plays_per_game"]
        league_avg_eff = defaults["yards_per_play"]
    elif sport_enum == Sport.MLB:
        league_avg_pace = defaults["runs_per_game"]  # PA proxy
        league_avg_eff = defaults["runs_per_game"]
    elif sport_enum == Sport.NHL:
        league_avg_pace = defaults["shots_per_game"]
        league_avg_eff = defaults["goals_per_game"]
    elif sport_enum == Sport.SOCCER:
        league_avg_pace = defaults["shots_per_game"]
        league_avg_eff = defaults["xg_per_game"]
    else:
        return []

    # Compute environment adjustment (venue + weather + refs)
    env_adj = 0.0
    env_detail = None
    env_sport_code = pace_sport.upper()
    if venue_team:
        try:
            env_result = total_environment_adjustment(
                venue=venue_team,
                sport=env_sport_code,
                weather=weather_data,
                refs=refs,
            )
            env_adj = env_result.get("total_adj", 0.0)
            env_detail = env_result
        except Exception as e:
            logger.debug(f"Environment adjustment failed: {e}")

    edges = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")
        if not home or not away:
            continue

        # Extract book total line from the game data
        book_total = None
        book_over_odds = None
        book_under_odds = None

        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] != "totals":
                    continue
                for o in mkt.get("outcomes", []):
                    point = o.get("point")
                    if point is None:
                        continue
                    if o.get("name", "").lower() == "over":
                        book_total = point
                        book_over_odds = o.get("price", -110)
                    elif o.get("name", "").lower() == "under":
                        book_under_odds = o.get("price", -110)
                if book_total is not None:
                    break
            if book_total is not None:
                break

        if book_total is None or book_over_odds is None or book_under_odds is None:
            continue

        # Project total using pace model with league average inputs
        # (We use league averages as a baseline; specific team data would improve this
        # when available from team stats tools)
        try:
            projection = project_game_total(
                home_pace=league_avg_pace,
                away_pace=league_avg_pace,
                home_off_eff=league_avg_eff,
                away_off_eff=league_avg_eff,
                home_def_eff=league_avg_eff,
                away_def_eff=league_avg_eff,
                league_avg_pace=league_avg_pace,
                sport=pace_sport,
                league_avg_eff=league_avg_eff,
            )
        except Exception as e:
            logger.debug(f"Pace model projection failed for {away} @ {home}: {e}")
            continue

        # Apply environment adjustment
        model_total = projection.projected_total + env_adj

        # Detect edge: model total vs book total
        try:
            edge = detect_total_edge(
                projected_total=model_total,
                book_total=book_total,
                book_over_odds=book_over_odds,
                book_under_odds=book_under_odds,
                sport=pace_sport,
                home_expected=projection.home_projected + (env_adj / 2.0),
                away_expected=projection.away_projected + (env_adj / 2.0),
            )
        except Exception as e:
            logger.debug(f"Total edge detection failed for {away} @ {home}: {e}")
            continue

        # Only report edges above 1% (model-based edges are noisier)
        if abs(edge.edge_pct) < 1.0:
            continue

        edge_dict = {
            "game": f"{away} @ {home}",
            "game_id": game.get("id", ""),
            "edge_type": "PACE_MODEL_TOTAL",
            "direction": edge.edge_direction,
            "edge_pct": edge.edge_pct,
            "model_total": round(model_total, 1),
            "book_total": book_total,
            "delta": round(model_total - book_total, 1),
            "over_probability": edge.over_probability,
            "under_probability": edge.under_probability,
            "kelly_fraction": edge.kelly_fraction,
            "ev": edge.ev,
            "pace_factor": projection.pace_factor,
            "methodology": projection.methodology,
            "environment_adj": round(env_adj, 2),
            "environment_detail": env_detail,
        }
        edges.append(edge_dict)

    # Sort by edge magnitude
    edges.sort(key=lambda x: abs(x["edge_pct"]), reverse=True)
    if edges:
        logger.info(
            f"Pace model total edges ({sport}): {len(edges)} found, "
            f"best edge: {edges[0]['edge_pct']:.1f}% {edges[0]['direction']}"
        )
    return edges


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

    # Pace model total edges (independent fair value estimate)
    pace_model_edges = scan_pace_model_total_edges(games, sport)
    report["pace_model_totals"] = pace_model_edges
    if pace_model_edges:
        logger.info(f"Pace model totals: {len(pace_model_edges)} edges found")

    # Dead number steals — find books sitting on dead numbers while others
    # are on key numbers for the same game (spreads only)
    _dn_sport = sport.lower() if sport else ""
    if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
        dead_steals = _scan_dead_number_steals(games, sport)
        report["dead_number_steals"] = dead_steals
        if dead_steals:
            logger.info(f"Dead number steals: {len(dead_steals)} found")
    else:
        report["dead_number_steals"] = []

    # Simulation-based edge validation — independently validate cross-book
    # edges using Monte Carlo simulations (simulate_spread for high-scoring,
    # compare_poisson_to_market for low-scoring sports)
    sim_validated = _simulation_validate_edges(games, sport, report)
    report["simulation_validated"] = sim_validated
    if sim_validated:
        logger.info(f"Simulation validation: {len(sim_validated)} edges validated")

    # Summary
    total_edges = sum(
        len(report.get(k, []))
        for k in report
        if k.startswith("cross_book_") or k.startswith("low_vig_") or k in ("pace_model_totals", "dead_number_steals", "simulation_validated")
    )
    report["total_edges"] = total_edges

    return report


def _simulation_validate_edges(games: list[dict], sport: str, report: dict) -> list[dict]:
    """
    Use Monte Carlo simulation to independently validate cross-book edges.

    For spread edges: run simulate_spread() and compare sim-implied prob vs book.
    For totals in low-scoring sports: run compare_poisson_to_market().
    Only validates edges that passed the cross-book divergence filter.
    """
    try:
        from tools.simulation import simulate_spread, simulate_poisson, compare_poisson_to_market, _classify_sport
    except ImportError:
        logger.debug("Simulation module not available for edge validation")
        return []

    validated = []
    classification = _classify_sport(sport) if sport else "high_scoring"

    # Only validate the top cross-book edges to avoid burning CPU
    spread_edges = report.get("cross_book_spreads", [])[:5]
    total_edges = report.get("cross_book_totals", [])[:5]

    # Build game lookup for simulation
    game_by_id = {g.get("id", ""): g for g in games}

    # Validate spread edges with simulate_spread
    for edge_info in spread_edges:
        game_id = edge_info.get("game_id", "")
        game = game_by_id.get(game_id)
        if not game:
            continue
        try:
            sim_result = simulate_spread(game, sport=sport, n_sims=5000)
            fair_spread = sim_result.get("fair_spread", 0)
            sim_edges = sim_result.get("edges", [])
            if sim_edges:
                best_sim_edge = sim_edges[0]
                if abs(best_sim_edge.get("edge", 0)) >= 0.02:
                    hold_info = _compute_market_hold(game, "spreads")
                    validated.append({
                        "source": "simulation",
                        "type": "spread",
                        "game": edge_info.get("game", ""),
                        "team": edge_info.get("team", ""),
                        "fair_spread": fair_spread,
                        "sim_edge": best_sim_edge.get("edge", 0),
                        "sim_edge_pct": best_sim_edge.get("edge_pct", 0),
                        "sim_prob": best_sim_edge.get("simulated_prob", 0),
                        "book_prob": best_sim_edge.get("book_prob", 0),
                        "rating": best_sim_edge.get("rating", "NO_EDGE"),
                        "cross_book_agrees": edge_info.get("implied_range", 0) >= 0.03,
                        **hold_info,
                    })
        except Exception as e:
            logger.debug(f"Sim validation failed for spread edge {game_id}: {e}")

    # Validate total edges for low-scoring sports with Poisson model
    if classification == "low_scoring":
        for edge_info in total_edges:
            game_id = edge_info.get("game_id", "")
            game = game_by_id.get(game_id)
            if not game:
                continue
            try:
                import numpy as np
                totals_found = []
                for bm in game.get("bookmakers", []):
                    for mkt in bm.get("markets", []):
                        if mkt["key"] == "totals":
                            for o in mkt.get("outcomes", []):
                                if o.get("point") is not None:
                                    totals_found.append(o["point"])
                if not totals_found:
                    continue
                consensus_total = float(np.median(totals_found))
                home_exp = consensus_total * 0.52
                away_exp = consensus_total * 0.48

                poisson_result = simulate_poisson(home_exp, away_exp)
                poisson_edges = compare_poisson_to_market(
                    poisson_result, game,
                    game.get("home_team", "Home"),
                    game.get("away_team", "Away"),
                )
                for pe in poisson_edges[:2]:
                    if abs(pe.get("edge", 0)) >= 0.02:
                        hold_info = _compute_market_hold(game, "totals")
                        validated.append({
                            "source": "poisson_simulation",
                            "type": "total",
                            "game": edge_info.get("game", ""),
                            "team": pe.get("team", ""),
                            "market": pe.get("market", "totals"),
                            "model_prob": pe.get("model_probability", 0),
                            "market_implied": pe.get("market_implied", 0),
                            "edge": pe.get("edge", 0),
                            "cross_book_agrees": edge_info.get("implied_range", 0) >= 0.03,
                            **hold_info,
                        })
            except Exception as e:
                logger.debug(f"Poisson validation failed for total edge {game_id}: {e}")

    return validated


def _compute_market_hold(game: dict, market_key: str) -> dict:
    """Compute overround and hold for a specific market using math_utils."""
    try:
        from tools.math_utils import calculate_overround, calculate_hold, american_to_decimal
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt["key"] == market_key:
                    outcomes = mkt.get("outcomes", [])
                    if len(outcomes) >= 2:
                        decimal_odds = [american_to_decimal(o.get("price", -110)) for o in outcomes]
                        return {
                            "overround": round(calculate_overround(decimal_odds), 4),
                            "hold": round(calculate_hold(decimal_odds), 4),
                        }
    except Exception:
        pass
    return {"overround": None, "hold": None}


def _scan_dead_number_steals(games: list[dict], sport: str) -> list[dict]:
    """Scan all games for dead number steal opportunities across books.

    For each game, collects spread lines from all books and runs
    find_dead_number_steals() to find books sitting on dead numbers
    while others are on key numbers.
    """
    all_steals = []

    for game in games:
        home = game.get("home_team", "")
        away = game.get("away_team", "")

        for team in [home, away]:
            if not team:
                continue

            best = find_best_line(game, market="spreads", team=team)
            all_lines = best.get("all_lines", [])
            if len(all_lines) < 2:
                continue

            # Build the lines list for find_dead_number_steals
            lines_for_dn = [
                {
                    "bookmaker": l["bookmaker"],
                    "spread": l.get("point", 0),
                    "price": l.get("price", -110),
                }
                for l in all_lines
                if l.get("point") is not None
            ]

            if len(lines_for_dn) < 2:
                continue

            try:
                steals = find_dead_number_steals(lines_for_dn, sport)
                for s in steals:
                    s["game"] = f"{away} @ {home}"
                    s["team"] = team
                all_steals.extend(steals)
            except (ValueError, KeyError):
                continue

    all_steals.sort(key=lambda x: x.get("prob_difference", 0), reverse=True)
    return all_steals
