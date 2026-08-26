"""Shared helpers for tools.edges submodules."""

import logging
import math
import os
from datetime import datetime, timezone
from typing import Optional

from tools.odds_api import calculate_implied_probability

logger = logging.getLogger("callisto.edge_scanner")

# Wiki confidence adjustment cap, configurable via env.
WIKI_EDGE_ADJUSTMENT_CAP = float(os.getenv("CALLISTO_WIKI_EDGE_CAP", "0.15"))

# Freshness weighting — exponential decay on odds-line age. A book with a
# 30s-old quote weighs ~6x more than one with a 3min-old quote. The half-life
# is configurable at import time via CALLISTO_ODDS_HALF_LIFE_S.
#
# Why 180s default? odds-api.io's WS path delivers updates in <1s; the 15min
# snapshot path delivers updates aged 0-900s. With HALF_LIFE=180 the mean age
# of a 15-min-old line contributes ~3% weight vs a fresh one, which is the
# right "almost ignore it but don't zero it out" behaviour for sharp consensus.
_DEFAULT_HALF_LIFE_S = 180.0
try:
    _ODDS_HALF_LIFE_S = float(os.getenv("CALLISTO_ODDS_HALF_LIFE_S", str(_DEFAULT_HALF_LIFE_S)))
    if _ODDS_HALF_LIFE_S <= 0:
        _ODDS_HALF_LIFE_S = _DEFAULT_HALF_LIFE_S
except (TypeError, ValueError):
    _ODDS_HALF_LIFE_S = _DEFAULT_HALF_LIFE_S

_DEBUG_WEIGHTS = os.getenv("CALLISTO_ODDS_DEBUG_WEIGHTS", "0") == "1"

from tools.odds_api import (
    calculate_ev,
    find_best_line,
)
from tools.market_microstructure import compute_market_metrics
from tools.book_keys import canonicalize_book
from tools.dead_numbers import (
    is_dead_number as _is_dead_number,
    key_number_value as _key_number_value,
    find_dead_number_steals,
    analyze_spread as _analyze_spread,
    rank_line_shopping_opportunities,
    buy_points_analysis,
    SPORT_ALIASES as _DEAD_NUM_SPORT_ALIASES,
)

def _parse_line_timestamp(line: dict) -> Optional[datetime]:
    """Best-effort parse of a line's freshness timestamp.

    Prefers `fetched_at` (our own ingest stamp) over `last_update` (the book's
    timestamp as reported by odds-api.io / the-odds-api.com). Our stamp is
    strictly more meaningful for consensus freshness — it answers "how long
    ago did I trust this number" rather than "what did the book claim".

    Returns None if no usable timestamp is present; callers fall back to
    "assume freshly fetched" (weight = 1.0) in that case, which keeps the
    scanner compatible with legacy snapshot rows that pre-date this column.
    """
    for key in ("fetched_at", "last_update"):
        val = line.get(key)
        if not val:
            continue
        try:
            if isinstance(val, datetime):
                dt = val
            else:
                s = str(val)
                if s.endswith("Z"):
                    s = s[:-1] + "+00:00"
                dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            continue
    return None


def _freshness_weight(line: dict, now: Optional[datetime] = None,
                      half_life_s: Optional[float] = None) -> float:
    """Compute an exponential-decay freshness weight for a line.

    weight = exp(-age_seconds / half_life). Clamped to [1e-4, 1.0]; an
    unknown/negative age gets weight 1.0 so we don't silently zero out
    legacy data.
    """
    if half_life_s is None:
        half_life_s = _ODDS_HALF_LIFE_S
    ts = _parse_line_timestamp(line)
    if ts is None:
        return 1.0
    if now is None:
        now = datetime.now(timezone.utc)
    age = (now - ts).total_seconds()
    if age <= 0:
        return 1.0
    try:
        w = math.exp(-age / half_life_s)
    except (ValueError, OverflowError):
        return 1e-4
    # Floor at 1e-4 so an ancient line still contributes epsilon instead of
    # disappearing entirely — the upstream sanity checks still gate extreme
    # cases, and a hard zero can misbehave in weighted-sum divisions.
    return max(1e-4, min(1.0, w))


def weighted_sharp_consensus(
    sharp_lines: list[dict],
    *,
    half_life_s: Optional[float] = None,
    now: Optional[datetime] = None,
    debug: bool = False,
) -> tuple[Optional[float], list[dict]]:
    """Freshness-weighted mean implied probability across sharp books.

    Returns (consensus_prob_or_None, per_line_debug). The debug list
    contains one dict per input line with keys: bookmaker, implied, age_s,
    weight. Useful for logging/telemetry without having to recompute.

    If all weights are zero (shouldn't happen given the floor, but defend
    against it) or sharp_lines is empty, returns None.
    """
    if not sharp_lines:
        return None, []

    if now is None:
        now = datetime.now(timezone.utc)
    if half_life_s is None:
        half_life_s = _ODDS_HALF_LIFE_S

    rows: list[dict] = []
    total_w = 0.0
    total_wp = 0.0
    for l in sharp_lines:
        implied = calculate_implied_probability(l["price"])
        w = _freshness_weight(l, now=now, half_life_s=half_life_s)
        ts = _parse_line_timestamp(l)
        age_s = (now - ts).total_seconds() if ts is not None else None
        rows.append({
            "bookmaker": l.get("bookmaker", ""),
            "implied": round(implied, 4),
            "age_s": round(age_s, 1) if age_s is not None else None,
            "weight": round(w, 4),
        })
        total_w += w
        total_wp += w * implied

    if total_w <= 0:
        return None, rows

    consensus = total_wp / total_w
    if debug or _DEBUG_WEIGHTS:
        logger.debug(f"Freshness-weighted consensus: {consensus:.4f} from {rows}")
    return consensus, rows


def _filter_in_progress_games(games: list[dict]) -> list[dict]:
    """Remove games whose commence_time has passed (in-progress or finished).

    Live odds contaminate sharp consensus and produce phantom edges
    (see BAL@PIT 2026-04-04 incident). This is defense-in-depth — callers
    like full_edge_scan() also filter, but individual scan functions must
    be self-protective so direct API callers can't bypass the gate.
    """
    now = datetime.now(timezone.utc)
    pre_game = []
    for g in games:
        ct = g.get("commence_time")
        if ct:
            try:
                if isinstance(ct, str):
                    ct_dt = datetime.fromisoformat(ct.replace("Z", "+00:00"))
                else:
                    ct_dt = ct
                if ct_dt.tzinfo is None:
                    ct_dt = ct_dt.replace(tzinfo=timezone.utc)
                if ct_dt <= now:
                    continue
            except (ValueError, TypeError):
                pass  # Can't parse — include to be safe
        pre_game.append(g)
    return pre_game

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

def _scan_line_group(
    *,
    edges: list,
    lines: list[dict],
    game: dict,
    home: str,
    away: str,
    team: str,
    market: str,
    sport: str,
    SHARP_TITLES: set,
    SOFT_TITLES: set,
) -> None:
    """Score one group of lines (same market, same team, same point value).

    Callers must pre-group the lines; this function trusts that every entry
    in `lines` is comparable and applies:
      - H2H contamination check
      - Implied-range sanity check (12%)
      - FRESHNESS-WEIGHTED sharp consensus (exponential decay, half-life
        from CALLISTO_ODDS_HALF_LIFE_S)
      - Soft-book edge detection vs the weighted consensus

    Edge-sanity caps remain unchanged so a bad snapshot can't blow past
    the 20%/12% guardrails.
    """
    if len(lines) < 2:
        return
    all_lines = lines
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
            return

    # Calculate implied probability range across books
    implied_probs = [calculate_implied_probability(l["price"]) for l in all_lines]
    implied_range = max(implied_probs) - min(implied_probs)
    avg_implied = sum(implied_probs) / len(implied_probs)

    # Sanity: implied range > 12% is almost certainly data contamination.
    # With freshness weighting below we could in principle relax this cap
    # (scraper-vs-API drift shouldn't blow it past 12%), but leaving it as-is
    # is defense-in-depth — a 12% range with identical timestamps is still
    # a contaminated-data signal.
    if implied_range > 0.12:
        logger.warning(
            f"Implausible implied range {implied_range:.1%} for {team} "
            f"{market} — likely data contamination, skipping"
        )
        return

    # Classify which books are sharp vs soft for this line. Use
    # canonicalize_book so "Betfair Exchange" / "betfair exchange" /
    # "betfair_exchange" all collapse onto the same key before the
    # membership test — previously the raw .lower() left spaces intact
    # and silently dropped odds-api.io's underscore form.
    def _is_sharp(l: dict) -> bool:
        bk = canonicalize_book(l.get("bookmaker", ""))
        return (
            bk in SHARP_TITLES
            or l.get("bookmaker", "").lower() in SHARP_TITLES
        )

    def _is_soft(l: dict) -> bool:
        bk = canonicalize_book(l.get("bookmaker", ""))
        return (
            bk in SOFT_TITLES
            or l.get("bookmaker", "").lower() in SOFT_TITLES
        )

    sharp_lines = [l for l in all_lines if _is_sharp(l)]
    soft_lines = [l for l in all_lines if _is_soft(l)]

    # Sharp consensus = FRESHNESS-WEIGHTED mean of sharp book implied probs.
    # A book quoting 30s ago weighs ~6x more than one quoting 3min ago.
    sharp_consensus, weight_debug = weighted_sharp_consensus(sharp_lines)
    if _DEBUG_WEIGHTS and sharp_consensus is not None:
        logger.info(
            f"[weights] {team} {market}: consensus={sharp_consensus:.4f} "
            f"weights={weight_debug}"
        )

    # Freshness metadata for consumers — average/max age of the books used.
    avg_age_s: Optional[float] = None
    max_age_s: Optional[float] = None
    if weight_debug:
        ages = [r["age_s"] for r in weight_debug if r["age_s"] is not None]
        if ages:
            avg_age_s = round(sum(ages) / len(ages), 1)
            max_age_s = round(max(ages), 1)

    # Edge: soft book offers better price than sharp consensus
    soft_edges = []
    if sharp_consensus is not None:
        for sl in soft_lines:
            soft_implied = calculate_implied_probability(sl["price"])
            edge = sharp_consensus - soft_implied
            if edge > 0.20:
                logger.warning(
                    f"Implausible edge {edge:.1%} for {team} at "
                    f"{sl['bookmaker']} — likely data contamination"
                )
                continue
            if edge > 0.02:
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

    if not (price_spread >= 10 or implied_range >= 0.03):
        return

    # Compute market microstructure metrics
    book_name_list = [l["bookmaker"] for l in all_lines]
    micro = compute_market_metrics(implied_probs, book_name_list, SHARP_TITLES)

    # Dead number / key number enrichment for spreads/totals
    dead_num_info: dict = {}
    if market in ("spreads", "totals") and sport:
        _dn_sport = sport.lower()
        if _dn_sport in _DEAD_NUM_SPORT_ALIASES:
            best_point = best_line.get("point")
            if best_point is not None:
                try:
                    dead_num_info["is_dead_number"] = _is_dead_number(best_point, _dn_sport)
                    dead_num_info["key_number_importance"] = _key_number_value(best_point, _dn_sport)
                except (ValueError, KeyError):
                    pass

    # Line shopping analysis: compare best vs worst spread across books
    line_shopping_info: dict = {}
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

    fair_probs = None
    market_hold = None
    try:
        from tools.math_utils import no_vig_price as _nvp, calculate_hold as _ch, american_to_decimal as _atd
        if len(all_lines) >= 2:
            fair_probs = _nvp(best_line["price"], worst_line["price"])
            dec_odds = [_atd(l["price"]) for l in all_lines[:2]]
            market_hold = round(_ch(dec_odds), 4)
    except Exception:
        pass

    edges.append({
        "game": f"{away} @ {home}",
        "game_id": game.get("id", ""),
        "commence_time": game.get("commence_time"),
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
        "sharp_consensus_weighted": True,
        "consensus_avg_age_s": avg_age_s,
        "consensus_max_age_s": max_age_s,
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
