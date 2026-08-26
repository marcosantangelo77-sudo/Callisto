"""Hypothesis line-filter parsing and condition matching.

Extracted verbatim from tools/backtest_io.py. The context-factor registries
(UNFILTERABLE_CONTEXT_FACTORS / FILTERABLE_CONTEXT_FACTORS /
_CONTEXT_KEYWORD_MAP) remain canonically defined in tools.backtest_io and
are imported at call time to avoid import cycles.
"""

import re
from typing import Optional

logger = __import__("logging").getLogger("callisto.backtest")


def has_structured_filters(config: dict) -> bool:
    """Check if hypothesis has line_filters or game_filters that differentiate event sets.

    When structured filters are present, the hypothesis can meaningfully
    differentiate games even without context factor filtering. This prevents
    the keyword-based context inference from blocking otherwise testable hypotheses.
    """
    lf = config.get("line_filters") or {}
    gf = config.get("game_filters") or {}
    return bool(
        any(v for v in lf.values() if v)
        or any(v is not None for v in gf.values())
    )

def _infer_context_needs(thesis: str, name: str) -> list[str]:
    """Infer unfilterable context factors from thesis/name when context_factors is empty.

    Returns list of inferred context needs, or empty list if the hypothesis
    appears to be purely line-based (no game-context filtering needed).

    Only returns factors that are in UNFILTERABLE_CONTEXT_FACTORS — keywords
    that map to derivable/filterable factors should not block backtesting.
    """
    from tools import backtest_io

    # Replace underscores/hyphens with spaces so \b word boundaries match
    # hypothesis names like "sp_dome_to_cold" (where _ is a word char in regex)
    text = f"{thesis} {name}".lower().replace("_", " ").replace("-", " ")
    inferred = set()
    for pattern, factor in backtest_io._CONTEXT_KEYWORD_MAP.items():
        if re.search(pattern, text):
            # Only block if the factor is truly unfilterable
            if factor in backtest_io.UNFILTERABLE_CONTEXT_FACTORS:
                inferred.add(factor)
    return sorted(inferred)


def _parse_hypothesis_filters(thesis: str, config: dict, hypothesis_id: str = "") -> dict:
    """Parse hypothesis thesis text, model_config, and name to extract line-based filters.

    Returns a dict of filters that can be applied to game lines:
        side_filter: "Over", "Under", or None — only evaluate this side
        spread_range: (min, max) or None — only test spreads in this range
        spread_min: float or None — only test spreads >= this value
        home_away_filter: "home", "away", or None — only test this side
        dog_fav_filter: "underdog", "favorite", or None — only test this role
    """
    filters = {}
    thesis_lower = thesis.lower() if thesis else ""
    h_id_lower = hypothesis_id.lower() if hypothesis_id else ""

    # 0. STRUCTURED LINE FILTERS from model_config (highest priority)
    # These are machine-readable specs generated alongside the hypothesis.
    # When present, use them directly and skip all regex parsing.
    lf = config.get("line_filters") or {}
    if lf:
        if lf.get("home_away") in ("home", "away"):
            filters["home_away_filter"] = lf["home_away"]
        if lf.get("dog_fav") in ("underdog", "favorite"):
            filters["dog_fav_filter"] = lf["dog_fav"]
        if lf.get("side") in ("Over", "Under"):
            filters["side_filter"] = lf["side"]
        if lf.get("spread_range") and isinstance(lf["spread_range"], (list, tuple)):
            lo, hi = float(lf["spread_range"][0]), float(lf["spread_range"][1])
            if lo > hi:
                lo, hi = hi, lo
            filters["spread_range"] = (lo, hi)
        if lf.get("spread_min") is not None:
            filters["spread_min"] = float(lf["spread_min"])
        if filters:
            logger.info(
                f"Line filters from structured spec for {hypothesis_id}: {filters}"
            )
            return filters  # Structured filters are authoritative

    # 1. Side filter from model_config (most reliable — explicitly set)
    side_filter = config.get("side_filter")
    if side_filter:
        filters["side_filter"] = side_filter  # "Over" or "Under"
    elif config.get("market_type") == "totals" or "total" in thesis_lower:
        # Parse from thesis: if thesis is about "under" or "over" specifically
        # Be careful: "underdog" contains "under" but is not a side filter
        # Look for "under" as a side-of-total context, not "underdog"
        thesis_words = re.split(r'[\s,;.!?()]+', thesis_lower)
        has_under_side = ("under" in thesis_words or "unders" in thesis_words
                         or "under-" in thesis_lower
                         or re.search(r'\bunder\b(?!dog)', thesis_lower))
        has_over_side = ("over" in thesis_words or "overs" in thesis_words
                        or "over-" in thesis_lower
                        or re.search(r'\bover\b(?!reaction|priced|weight|all|val)', thesis_lower))
        # Only set side filter if thesis is clearly about ONE side
        if has_under_side and not has_over_side:
            filters["side_filter"] = "Under"
        elif has_over_side and not has_under_side:
            filters["side_filter"] = "Over"

    # 1b. Fallback: extract side from hypothesis NAME if thesis parsing missed it.
    # Names like "mlb_opener_bullpen_game_total_over" or "mlb_new_manager_total_under"
    # encode the predicted direction. Only use as fallback when thesis didn't yield a side.
    if "side_filter" not in filters and h_id_lower:
        is_totals = config.get("market_type") == "totals" or "total" in h_id_lower
        if is_totals:
            # Check name suffix/segments for over/under direction
            name_parts = h_id_lower.replace("-", "_").split("_")
            name_has_over = "over" in name_parts or "overs" in name_parts
            name_has_under = "under" in name_parts or "unders" in name_parts
            if name_has_over and not name_has_under:
                filters["side_filter"] = "Over"
                logger.info(f"Side filter 'Over' inferred from hypothesis name: {hypothesis_id}")
            elif name_has_under and not name_has_over:
                filters["side_filter"] = "Under"
                logger.info(f"Side filter 'Under' inferred from hypothesis name: {hypothesis_id}")

    # 2. Spread range from model_config or thesis
    spread_range = config.get("spread_range")
    if spread_range and isinstance(spread_range, (list, tuple)) and len(spread_range) == 2:
        filters["spread_range"] = (float(spread_range[0]), float(spread_range[1]))
    else:
        # Parse "X-Y points" from thesis — only when describing game selection criteria
        # e.g., "underdogs of 3-7 points" or "favorites by 3-7 points"
        # NOT "moving the line 0.5-1.5 points past fair value" (line movement context)
        range_match = re.search(
            r'(?:of|by|between|within|spread(?:s)?[\s:]+)'
            r'\s*(\d+(?:\.\d+)?)\s*[-–to]+\s*(\d+(?:\.\d+)?)\s*point',
            thesis_lower,
        )
        if range_match:
            low = float(range_match.group(1))
            high = float(range_match.group(2))
            if low > high:
                low, high = high, low
            # Sanity: spread ranges should be in a reasonable range (1-20)
            if 1 <= low and high <= 25:
                filters["spread_range"] = (low, high)

    # 3. Spread minimum from thesis ("X+ points", "of X+ points")
    if "spread_range" not in filters:
        # Only match when in a game-selection context, not line movement
        min_match = re.search(
            r'(?:of|by|at least|minimum|spread(?:s)?[\s:]+)'
            r'\s*(\d+(?:\.\d+)?)\+?\s*point',
            thesis_lower,
        )
        if min_match:
            val = float(min_match.group(1))
            # Sanity: only treat as spread minimum if it looks like a spread value (1-20)
            if 1 <= val <= 20:
                filters["spread_min"] = val

    # 4. Home/away filter — from thesis text
    if re.search(r'\bhome\s+(underdog|dog|team|favorite|side|advantage)', thesis_lower):
        filters["home_away_filter"] = "home"
    elif re.search(r'\b(?:road|away|visitor|visiting)\s+(underdog|dog|team|favorite|side|value)', thesis_lower):
        filters["home_away_filter"] = "away"
    elif re.search(r'\baway\s+(underdog|dog|team|favorite)', thesis_lower):
        filters["home_away_filter"] = "away"

    # 4b. Fallback: extract home/away from hypothesis NAME.
    # Names like "mlb_opening_week_road_favorites_h2h" or "nba_home_underdog_ats"
    if "home_away_filter" not in filters and h_id_lower:
        name_parts = h_id_lower.replace("-", "_").split("_")
        if any(kw in name_parts for kw in ("road", "away", "visitor", "visiting")):
            filters["home_away_filter"] = "away"
            logger.info(f"home_away_filter 'away' inferred from hypothesis name: {hypothesis_id}")
        elif "home" in name_parts:
            filters["home_away_filter"] = "home"
            logger.info(f"home_away_filter 'home' inferred from hypothesis name: {hypothesis_id}")

    # 5. Underdog/favorite filter — from thesis text
    if re.search(r'\bunderdog', thesis_lower) and not re.search(r'\bfavorite', thesis_lower):
        filters["dog_fav_filter"] = "underdog"
    elif re.search(r'\bfavorite', thesis_lower) and not re.search(r'\bunderdog', thesis_lower):
        filters["dog_fav_filter"] = "favorite"

    # 5b. Fallback: extract dog/fav from hypothesis NAME if thesis didn't yield it.
    # Names like "mlb_opening_week_underdog_ml" or "mlb_road_favorite_mispricing"
    # encode the predicted direction.
    if "dog_fav_filter" not in filters and h_id_lower:
        name_parts = set(h_id_lower.replace("-", "_").split("_"))
        has_dog = bool(name_parts & {
            "underdog", "dog", "underdogs", "undervalued", "upset",
        })
        has_fav = bool(name_parts & {
            "favorite", "favorites", "fav", "chalk",
        })
        if has_dog and not has_fav:
            filters["dog_fav_filter"] = "underdog"
            logger.info(f"dog_fav_filter 'underdog' inferred from hypothesis name: {hypothesis_id}")
        elif has_fav and not has_dog:
            filters["dog_fav_filter"] = "favorite"
            logger.info(f"dog_fav_filter 'favorite' inferred from hypothesis name: {hypothesis_id}")

    # 6. Thesis direction — detect bearish hypotheses (bet AGAINST filtered side)
    # E.g., "nba_heavy_favorite_ml_overpriced" → bet the underdog, not the favorite.
    # When bearish + a directional filter exists, flip the filter so we
    # evaluate and record the CORRECT bet side.
    #
    # IMPORTANT: Only use the hypothesis NAME for bearish detection, not the
    # thesis text. The name encodes intent (what we bet on), while the thesis
    # explains the phenomenon. A thesis like "underdogs have value because
    # favorites are overpriced" is BULLISH on underdogs — detecting "overpriced"
    # in the thesis would falsely trigger a bearish flip.
    bearish_name = False
    if h_id_lower:
        name_parts = set(h_id_lower.replace("-", "_").split("_"))
        bearish_name = bool(name_parts & {
            "overpriced", "overvalued", "inflated",
            "fade", "fading",
        })

    if bearish_name:
        flipped = False
        if "dog_fav_filter" in filters:
            old = filters["dog_fav_filter"]
            filters["dog_fav_filter"] = (
                "underdog" if old == "favorite" else "favorite"
            )
            flipped = True
            logger.info(
                "Bearish thesis detected for %s: flipped dog_fav_filter "
                "%s → %s", hypothesis_id, old, filters["dog_fav_filter"],
            )
        if "home_away_filter" in filters:
            old = filters["home_away_filter"]
            filters["home_away_filter"] = (
                "away" if old == "home" else "home"
            )
            flipped = True
            logger.info(
                "Bearish thesis detected for %s: flipped home_away_filter "
                "%s → %s", hypothesis_id, old, filters["home_away_filter"],
            )
        if flipped:
            filters["_bearish_flip"] = True

    return filters


def matches_hypothesis_conditions(
    side_name: str,
    market_type: str,
    point: Optional[float],
    home_team: str,
    away_team: str,
    filters: dict,
    fair_prob: Optional[float] = None,
) -> bool:
    """Check if a specific line/side matches the hypothesis conditions.

    Args:
        side_name: The side being evaluated (team name, "Over", "Under")
        market_type: "spreads", "totals", "h2h"
        point: The line value (spread number, total number)
        home_team: Home team name
        away_team: Away team name
        filters: Pre-parsed filters from _parse_hypothesis_filters()
        fair_prob: Devigged fair probability for this side (used for
            h2h favorite/underdog detection where no spread line exists)

    Returns True if this line should be processed, False to skip.
    """
    from tools.btio.teams import _team_matches

    # 1. Side filter (Over/Under for totals) — check even when filters dict
    # is empty, because totals should default to single-side if possible
    side_filter = filters.get("side_filter") if filters else None
    if market_type == "totals":
        if side_filter:
            if side_name.lower() != side_filter.lower():
                return False
        # No side filter on a totals hypothesis = both sides processed.
        # This is acceptable for generic edge detection but will be flagged
        # in backtest metadata as "unfiltered_totals_side".

    if not filters:
        # No line-based filters parsed — process all lines for this game.
        # WARNING: This means the hypothesis has no directional filtering.
        # For generic cross-book edge detection this is acceptable, but for
        # directional hypotheses (favorite/underdog/home/away) this is a bug
        # in _parse_hypothesis_filters that causes identical event sets.
        # We still return True here but callers should check filter coverage.
        return True

    # 2. Spread range filter
    spread_range = filters.get("spread_range")
    if spread_range and market_type == "spreads" and point is not None:
        abs_spread = abs(point)
        low, high = spread_range
        if abs_spread < low or abs_spread > high:
            return False

    # 3. Spread minimum filter
    spread_min = filters.get("spread_min")
    if spread_min is not None and market_type == "spreads" and point is not None:
        if abs(point) < spread_min:
            return False

    # 4. Home/away filter
    home_away = filters.get("home_away_filter")
    if home_away and market_type in ("spreads", "h2h"):
        is_home_side = _team_matches(side_name, home_team)
        is_away_side = _team_matches(side_name, away_team)
        if home_away == "home" and not is_home_side:
            return False
        if home_away == "away" and not is_away_side:
            return False

    # 5. Underdog/favorite filter
    # For spreads: negative line = favorite, positive = underdog
    # For h2h: fair_prob > 0.5 = favorite, < 0.5 = underdog
    dog_fav = filters.get("dog_fav_filter")
    if dog_fav:
        if market_type == "spreads" and point is not None:
            is_underdog = point > 0
            is_favorite = point < 0
            if dog_fav == "underdog" and not is_underdog:
                return False
            if dog_fav == "favorite" and not is_favorite:
                return False
        elif market_type == "h2h" and fair_prob is not None:
            is_favorite = fair_prob > 0.5
            is_underdog = fair_prob < 0.5
            if dog_fav == "underdog" and not is_underdog:
                return False
            if dog_fav == "favorite" and not is_favorite:
                return False

    return True

def _log_unfilterable_context_factors(hypothesis_id: str, config: dict) -> list[str]:
    """Check for context factors we cannot filter on and log them.

    Returns list of unfilterable factors for inclusion in backtest metadata.
    """
    from tools import backtest_io

    context_factors = config.get("context_factors", [])
    if not context_factors:
        return []

    unfilterable = [
        f for f in context_factors
        if f.lower().replace(" ", "_") in backtest_io.UNFILTERABLE_CONTEXT_FACTORS
        or f.lower() in backtest_io.UNFILTERABLE_CONTEXT_FACTORS
    ]

    if unfilterable:
        logger.warning(
            f"Hypothesis {hypothesis_id} requires context_factors "
            f"{unfilterable} which are not yet available — running "
            f"unfiltered backtest for those conditions (results will be noisy)."
        )

    return unfilterable


def compute_context_coverage(config: dict) -> float:
    """Calculate what fraction of a hypothesis's context conditions can be filtered.

    Returns 1.0 if no context factors needed (pure line-based hypothesis),
    0.0 if ALL context factors are unfilterable (backtest is meaningless),
    or a value between 0-1 indicating partial coverage.

    Hypotheses with context_coverage < 0.5 should NOT be backtested — the
    results will be indistinguishable from random since most game-selection
    conditions cannot be applied.
    """
    from tools import backtest_io

    context_factors = config.get("context_factors", [])
    if not context_factors:
        return 1.0  # No context needed — pure line-based, fully filterable

    # WHITELIST logic: only factors with actual filtering code count as filterable.
    # Previously used blacklist (UNFILTERABLE), but unknown factors like
    # "season_week", "park_type" slipped through as falsely "filterable".
    filterable_count = sum(
        1 for f in context_factors
        if f.lower().replace(" ", "_") in backtest_io.FILTERABLE_CONTEXT_FACTORS
        or f.lower() in backtest_io.FILTERABLE_CONTEXT_FACTORS
    )

    return filterable_count / len(context_factors)
