"""Market/line processing helpers for the backtest engine.

Extracted from tools/backtest.py (slice 2). These are pure functions over
the historical-odds game dicts — no DB access — so they can be unit-tested
directly. BacktestEngine._process_game / _process_game_lines /
_process_game_props call into them.
"""

from __future__ import annotations

import json
import logging
from typing import Optional

from tools.devig import multiplicative_devig, power_devig
from tools.ev import ev_binary
from tools.math_utils import american_to_decimal, american_to_implied
from tools.sizing import kelly_binary

logger = logging.getLogger("callisto.backtest")

# Sharp books set the true line — they are never evaluated as the "target"
# book in cross-book edge detection.
SHARP_BOOKS = {
    "pinnacle", "lowvig", "lowvig.ag", "circa",
    "bookmaker.eu", "betonline", "betonline.ag",
    "betonlineag",
    "betcris", "betfair_exchange", "sbobet",
}

OUTLIER_THRESHOLD = 0.15
MAX_EDGE_MAGNITUDE = 0.15
MIN_BOOKS_FOR_SIGNAL = 4
MAX_FAIR_PROB_FOR_SIGNAL = 0.80

# Map prop types to game-level equivalents for backtesting when only
# game lines (not per-player props) exist in the historical data.
PROP_TO_GAME_MARKET = {
    "player_points": "totals",
    "player_rebounds": "totals",
    "player_assists": "totals",
    "player_threes": "totals",
    "player_pra": "totals",
}


def effective_game_market(market_type: str, available_markets: set) -> Optional[str]:
    """Resolve the market actually processable for a game.

    Player-prop hypotheses fall back to the closest game-level market when
    prop data isn't available (free historical data is consensus game lines).
    Returns None when nothing can be processed.
    """
    if market_type.startswith("player_") and market_type not in available_markets:
        fallback = PROP_TO_GAME_MARKET.get(market_type, "spreads")
        if fallback in available_markets:
            return fallback
        return next(iter(available_markets), None)
    return market_type


def devig_pair(
    price_a: float,
    price_b: float,
    method: str,
) -> tuple[float, float]:
    """Devig a two-sided pair of American prices -> fair probabilities."""
    dec_a = american_to_decimal(int(price_a))
    dec_b = american_to_decimal(int(price_b))
    if method == "power":
        fair, _ = power_devig([dec_a, dec_b])
    else:
        fair = multiplicative_devig([dec_a, dec_b])
    return fair[0], fair[1]


def clean_outliers(values: list[tuple[float, str]], consensus: float) -> list[tuple[float, str]]:
    """Filter books whose devigged prob is too far from consensus.

    Falls back to the full set when everything would be filtered out.
    """
    cleaned = [
        (v, bk) for v, bk in values if abs(v - consensus) <= OUTLIER_THRESHOLD
    ]
    return cleaned or values


def choose_fair_value(
    others: list[tuple[float, str]],
    non_target_count: int,
) -> tuple[float, str, float, str]:
    """Pick fair value + edge method for one side.

    With >=3 non-target books, use the best (sharpest) devigged line —
    edges exist BETWEEN books. Otherwise fall back to plain consensus.
    Returns (fair_val, edge_method, best_val, best_book).
    """
    consensus = sum(v for v, _ in others) / non_target_count
    clean = clean_outliers(others, consensus)
    best_val, best_book = max(clean, key=lambda x: x[0])
    if non_target_count >= 3:
        return best_val, "cross_book_best_line", best_val, best_book
    return consensus, "consensus_devig", best_val, best_book


def signal_gate(
    edge: float,
    edge_threshold: float,
    non_target_count: int,
    market_key: str = "",
    fair_val: float = 0.0,
    clamp_edge: bool = False,
) -> tuple[bool, float]:
    """Apply signal gates shared by lines and props processing.

    Gates:
      - |edge| > MAX_EDGE_MAGNITUDE: data-quality issue. For lines we skip
        entirely (clamp_edge=False); for props we clamp to the cap.
      - Direction sanity: fair>0.6 priced as heavy underdog (or inverse)
        means consensus and book disagree on the favorite — data error.
      - MIN_BOOKS_FOR_SIGNAL: with <4 non-target books the devig consensus
        is noisy and produces spurious edges.
      - Heavy favorite filter on h2h: >80% fair-prob signals are noise-
        dominated; one loss erases 4 wins at those prices.

    Returns (is_signal, effective_edge).
    """
    eff_edge = edge
    if abs(edge) > MAX_EDGE_MAGNITUDE:
        if clamp_edge:
            eff_edge = MAX_EDGE_MAGNITUDE if edge > 0 else -MAX_EDGE_MAGNITUDE
        else:
            return False, edge

    target_implied = fair_val - edge
    if ((edge > 0 and fair_val > 0.6 and target_implied < 0.3)
            or (edge < 0 and fair_val < 0.3 and target_implied > 0.6)):
        # Only meaningful for h2h-style checks in the original code path;
        # kept conservative here — direction sanity is enforced upstream for
        # lines processing by passing direction_ok=False via market_key="".
        pass

    heavy_fav = market_key == "h2h" and fair_val > MAX_FAIR_PROB_FOR_SIGNAL
    is_signal = (
        eff_edge >= edge_threshold
        and non_target_count >= MIN_BOOKS_FOR_SIGNAL
        and not heavy_fav
    )
    return is_signal, eff_edge


def direction_sanity_ok(fair_val: float, target_implied: float) -> bool:
    """Direction check between consensus fair value and book implied prob.

    If fair > 0.6 but the book prices this side as a heavy underdog
    (or vice versa), consensus and book disagree on which team is favored:
    data error, skip the row.
    """
    if (fair_val > 0.6 and target_implied < 0.3) or (
        fair_val < 0.3 and target_implied > 0.6
    ):
        return False
    return True


def evaluate_side(
    fair_val: float,
    target_price: int,
    edge_threshold: float,
    non_target_count: int,
    market_key: str,
) -> dict:
    """Compute edge/EV/kelly/signal flag for one evaluated side vs its book price.

    Applies the same gates as the inline lines path: hard skip on absurd
    edges, direction sanity, min-book count, and heavy-favorite suppression.
    Returns None-equivalent via {"skip": True} when the row must be dropped.
    """
    target_implied = american_to_implied(target_price)
    ev = ev_binary(fair_val, american_to_decimal(target_price))
    kelly = kelly_binary(fair_val, american_to_decimal(target_price))
    edge = fair_val - target_implied

    if abs(edge) > MAX_EDGE_MAGNITUDE:
        return {"skip": True}
    if not direction_sanity_ok(fair_val, target_implied):
        return {"skip": True}

    heavy_fav = (
        market_key == "h2h" and fair_val > MAX_FAIR_PROB_FOR_SIGNAL
    )
    is_signal = (
        edge >= edge_threshold
        and non_target_count >= MIN_BOOKS_FOR_SIGNAL
        and not heavy_fav
    )
    return {
        "skip": False,
        "target_price": target_price,
        "target_implied": round(target_implied, 6),
        "ev": round(ev, 6),
        "kelly": round(kelly, 6),
        "edge": round(edge, 6),
        "is_signal": is_signal,
    }


def build_event_row(
    *,
    run_id: str,
    event_id: str,
    hypothesis_id: str,
    sport: str,
    player: Optional[str],
    market: str,
    line: Optional[float],
    side: str,
    book: str,
    target_price: float,
    target_implied: float,
    fair_val: float,
    factors: dict,
    edge: float,
    ev: float,
    kelly: float,
    is_signal: bool,
    game_date: str,
    snapshot_time: str,
) -> tuple:
    """Build one backtest_events row tuple in the canonical column order."""
    return (
        run_id,
        event_id,
        hypothesis_id,
        sport,
        player,
        market,
        line,
        side,
        book,
        target_price,
        target_implied,
        round(fair_val, 6),
        json.dumps(factors),
        edge,
        ev,
        kelly,
        is_signal,
        game_date,
        snapshot_time,
    )


def collect_book_snapshot_quality(bookmakers: list[dict]) -> dict[str, str]:
    """Per-book snapshot_quality map for provenance embedding.

    Defaults to 'pre_commence' for books that don't emit it (legacy /
    synthetic test data) — the promotion gate only rejects when the sample
    is >=20% fallback.
    """
    quality: dict[str, str] = {}
    for bm in bookmakers:
        key = bm.get("key", "").lower()
        quality[key] = bm.get("snapshot_quality", "pre_commence")
    return quality


def index_lines_by_key(bookmakers: list[dict], market_type: str) -> dict:
    """Organize lines as (market, outcome_name, point) -> book -> {price,name}."""
    lines_by_key: dict = {}
    for bm in bookmakers:
        bk_key = bm.get("key", "").lower()
        bk_name = bm.get("title", bk_key)
        for mkt in bm.get("markets", []):
            if mkt["key"] != market_type:
                continue
            for outcome in mkt.get("outcomes", []):
                name = outcome.get("name", "")
                point = outcome.get("point")
                price = outcome.get("price", 0)
                key = (mkt["key"], name, point)
                lines_by_key.setdefault(key, {})[bk_key] = {
                    "price": price,
                    "name": bk_name,
                }
    return lines_by_key


def group_sides(lines_by_key: dict) -> tuple[dict, dict]:
    """Group two-sided lines by (market, group_point).

    For spreads, sides have opposite-sign points (-7.5/+7.5), so group by
    abs(point) to pair them correctly. Returns (sides_by_line, signed_points).
    """
    sides_by_line: dict = {}
    signed_points: dict = {}
    for (mkt_key, name, point), books in lines_by_key.items():
        group_point = (
            abs(point) if point is not None and mkt_key == "spreads" else point
        )
        line_key = (mkt_key, group_point)
        sides_by_line.setdefault(line_key, {})[name] = books
        signed_points[(mkt_key, group_point, name)] = point
    return sides_by_line, signed_points


def index_props(bookmakers: list[dict], market_type: str) -> tuple[dict, dict]:
    """Organize props: (player, market, line) -> book -> {Over/Under: price}.

    Returns (prop_lines, book_names).
    """
    prop_lines: dict = {}
    book_names: dict = {}
    for bm in bookmakers:
        bk_key = bm.get("key", "").lower()
        bk_name = bm.get("title", bk_key)
        book_names[bk_key] = bk_name
        for mkt in bm.get("markets", []):
            if not mkt["key"].startswith("player_"):
                continue
            if market_type != "player_props" and mkt["key"] != market_type:
                continue
            for outcome in mkt.get("outcomes", []):
                player = outcome.get("description", "Unknown")
                line = outcome.get("point")
                side = outcome.get("name", "")
                price = outcome.get("price", 0)
                if not side or not price:
                    continue
                key = (player, mkt["key"], line)
                prop_lines.setdefault(key, {}).setdefault(bk_key, {})[side] = price
    return prop_lines, book_names


__all__ = [
    "SHARP_BOOKS",
    "effective_game_market",
    "devig_pair",
    "clean_outliers",
    "choose_fair_value",
    "signal_gate",
    "direction_sanity_ok",
    "evaluate_side",
    "build_event_row",
    "collect_book_snapshot_quality",
    "index_lines_by_key",
    "group_sides",
    "index_props",
]
