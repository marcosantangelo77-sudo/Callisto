"""
AGP-compliant confidence scoring for detected betting edges.

Maps quantitative edge evidence to the AGP SourceClass/ConfidenceTier framework.
Every edge gets a confidence score that is CAPPED by the quality of its evidence:

    PRIMARY   (sharp book present)    → max 1.0  (VERIFIED)
    SECONDARY (soft book cross-ref)   → max 0.75 (CORROBORATED)
    SIGNAL    (single source)         → max 0.55 (PROBABLE)
    INFERRED  (no live data)          → max 0.55 (PROBABLE)

Within each ceiling, the score is determined by evidence strength:
    - Book count and agreement
    - Edge magnitude vs noise threshold
    - Market type efficiency
    - Cross-method consistency
"""

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("callisto.edge_confidence")

# AGP confidence ceilings (must match orchestrator.py)
CEILING_PRIMARY = 1.0
CEILING_SECONDARY = 0.75
CEILING_SIGNAL = 0.55
CEILING_INFERRED = 0.55

# Sharp book identifiers — matches both API keys and display titles (lowercase)
SHARP_BOOKS = {
    "pinnacle", "lowvig", "lowvig.ag", "lowvig.ag",
    "circa", "bookmaker.eu", "betonline", "betonline.ag",
    "betonlineag", "betcris",
}

# Edge must exceed this to be considered signal vs noise
NOISE_FLOOR_PCT = 0.5  # 0.5%

# Market efficiency tiers — less efficient markets can sustain edges longer
MARKET_EFFICIENCY = {
    "h2h": 0.95,           # Moneylines are highly efficient
    "spreads": 0.90,       # Spreads slightly less
    "totals": 0.85,        # Totals less efficient
    "player_points": 0.70, # Player props are where edges live
    "player_rebounds": 0.65,
    "player_assists": 0.65,
    "player_threes": 0.60,
    "player_points_rebounds_assists": 0.60,
    "alternate_spreads": 0.55,
    "alternate_totals": 0.55,
}


@dataclass
class EdgeConfidence:
    """AGP-scored confidence for a detected edge."""
    score: float
    tier: str           # VERIFIED, CORROBORATED, PROBABLE, SPECULATIVE, UNVERIFIED
    source_class: str   # PRIMARY, SECONDARY, SIGNAL, INFERRED
    ceiling: float
    factors: dict       # Breakdown of what contributed to the score
    reasoning: str      # Human-readable explanation


def score_edge(
    edge_pct: float,
    books_compared: int,
    book_names: list[str],
    market: str = "h2h",
    has_sharp_book: Optional[bool] = None,
    cross_method_confirmed: bool = False,
    is_live: bool = False,
    hours_to_game: Optional[float] = None,
) -> EdgeConfidence:
    """
    Score a detected edge using AGP confidence methodology.

    Args:
        edge_pct: Edge as a percentage (e.g., 3.2 for 3.2%)
        books_compared: Number of books in the devig/comparison
        book_names: List of book names/keys used
        market: Market type key (h2h, spreads, player_points, etc.)
        has_sharp_book: Override sharp detection (None = auto-detect from book_names)
        cross_method_confirmed: Edge found by multiple methods (devig + cross-book + simulation)
        is_live: Whether this is a live/in-play market
        hours_to_game: Hours until game starts (None if unknown)

    Returns:
        EdgeConfidence with AGP-compliant score, tier, and reasoning.
    """
    # Step 1: Determine source class from book quality
    if has_sharp_book is None:
        book_keys_lower = {b.lower() for b in book_names}
        has_sharp_book = bool(book_keys_lower & SHARP_BOOKS)

    if has_sharp_book:
        source_class = "PRIMARY"
        ceiling = CEILING_PRIMARY
    elif books_compared >= 2:
        source_class = "SECONDARY"
        ceiling = CEILING_SECONDARY
    elif books_compared == 1:
        source_class = "SIGNAL"
        ceiling = CEILING_SIGNAL
    else:
        source_class = "INFERRED"
        ceiling = CEILING_INFERRED

    factors = {}
    reasons = []

    # Step 2: Base score from edge magnitude
    if edge_pct >= 5.0:
        base = 0.90
        reasons.append(f"Strong edge ({edge_pct:.1f}%) — well above noise")
    elif edge_pct >= 3.0:
        base = 0.75
        reasons.append(f"Solid edge ({edge_pct:.1f}%) — clear signal")
    elif edge_pct >= 2.0:
        base = 0.60
        reasons.append(f"Moderate edge ({edge_pct:.1f}%) — actionable but monitor")
    elif edge_pct >= 1.0:
        base = 0.45
        reasons.append(f"Thin edge ({edge_pct:.1f}%) — could be noise")
    elif edge_pct >= NOISE_FLOOR_PCT:
        base = 0.30
        reasons.append(f"Marginal edge ({edge_pct:.1f}%) — likely noise")
    else:
        base = 0.15
        reasons.append(f"Sub-noise edge ({edge_pct:.1f}%) — not actionable")
    factors["edge_magnitude"] = round(base, 3)

    # Step 3: Book count adjustment
    if books_compared >= 5:
        book_adj = 0.10
        reasons.append(f"{books_compared} books — strong consensus")
    elif books_compared >= 3:
        book_adj = 0.05
        reasons.append(f"{books_compared} books — adequate sample")
    elif books_compared == 2:
        book_adj = 0.0
        reasons.append("2 books — minimum for cross-reference")
    else:
        book_adj = -0.10
        reasons.append("Single book — no cross-reference possible")
    factors["book_count"] = round(book_adj, 3)

    # Step 4: Sharp book bonus
    if has_sharp_book:
        sharp_adj = 0.10
        reasons.append("Sharp book (Pinnacle/LowVig) present — PRIMARY evidence")
    else:
        sharp_adj = 0.0
        reasons.append("No sharp books — capped at SECONDARY")
    factors["sharp_book"] = round(sharp_adj, 3)

    # Step 5: Market efficiency adjustment
    efficiency = MARKET_EFFICIENCY.get(market, 0.80)
    # Less efficient markets = edges more likely to be real
    market_adj = (1.0 - efficiency) * 0.15
    if efficiency <= 0.70:
        reasons.append(f"Prop market ({market}) — less efficient, edges persist longer")
    elif efficiency >= 0.90:
        reasons.append(f"Main line ({market}) — highly efficient, edge may close fast")
    factors["market_efficiency"] = round(market_adj, 3)

    # Step 6: Cross-method confirmation
    if cross_method_confirmed:
        method_adj = 0.08
        reasons.append("Edge confirmed by multiple methods — high conviction")
    else:
        method_adj = 0.0
    factors["cross_method"] = round(method_adj, 3)

    # Step 7: Live market penalty
    if is_live:
        live_adj = -0.10
        reasons.append("Live market — prices move fast, edge may be stale")
    else:
        live_adj = 0.0
    factors["live_penalty"] = round(live_adj, 3)

    # Step 8: Time-to-game adjustment
    time_adj = 0.0
    if hours_to_game is not None:
        if hours_to_game < 0.5:
            time_adj = 0.03  # Near tip — lines are sharp, edge is more meaningful
            reasons.append("Near game start — lines are sharpest, edge is meaningful")
        elif hours_to_game > 24:
            time_adj = -0.05  # Early line — may move
            reasons.append("24+ hours out — line may still move")
    factors["time_to_game"] = round(time_adj, 3)

    # Compute raw score
    raw = base + book_adj + sharp_adj + market_adj + method_adj + live_adj + time_adj
    # Clamp to [0, ceiling]
    score = round(max(0.0, min(raw, ceiling)), 3)
    factors["raw_total"] = round(raw, 3)
    factors["ceiling_applied"] = ceiling

    # Determine tier
    if score >= 0.90:
        tier = "VERIFIED"
    elif score >= 0.75:
        tier = "CORROBORATED"
    elif score >= 0.55:
        tier = "PROBABLE"
    elif score >= 0.30:
        tier = "SPECULATIVE"
    else:
        tier = "UNVERIFIED"

    reasoning = f"Source: {source_class} (ceiling {ceiling}). " + " | ".join(reasons)

    return EdgeConfidence(
        score=score,
        tier=tier,
        source_class=source_class,
        ceiling=ceiling,
        factors=factors,
        reasoning=reasoning,
    )


def score_parlay(leg_confidences: list[EdgeConfidence]) -> EdgeConfidence:
    """
    Score a parlay's overall confidence.

    Parlay confidence is limited by its weakest leg — a chain is only
    as strong as its weakest link. The score is the minimum leg score
    weighted by the geometric mean to account for cumulative risk.
    """
    if not leg_confidences:
        return EdgeConfidence(
            score=0.0, tier="UNVERIFIED", source_class="INFERRED",
            ceiling=0.55, factors={}, reasoning="No legs to score",
        )

    scores = [lc.score for lc in leg_confidences]
    min_score = min(scores)
    # Geometric mean biases toward the weakest leg
    product = 1.0
    for s in scores:
        product *= max(s, 0.01)  # avoid zero
    geo_mean = product ** (1.0 / len(scores))

    # Weighted: 60% weakest leg, 40% geometric mean
    combined = 0.6 * min_score + 0.4 * geo_mean
    # Parlay ceiling: lowest leg's ceiling
    ceiling = min(lc.ceiling for lc in leg_confidences)
    score = round(min(combined, ceiling), 3)

    # Determine tier
    if score >= 0.90:
        tier = "VERIFIED"
    elif score >= 0.75:
        tier = "CORROBORATED"
    elif score >= 0.55:
        tier = "PROBABLE"
    elif score >= 0.30:
        tier = "SPECULATIVE"
    else:
        tier = "UNVERIFIED"

    # Weakest leg source class
    source_class = min(leg_confidences, key=lambda lc: lc.score).source_class

    factors = {
        "leg_scores": scores,
        "min_score": round(min_score, 3),
        "geo_mean": round(geo_mean, 3),
        "leg_count": len(scores),
        "ceiling": ceiling,
    }

    weakest = min(leg_confidences, key=lambda lc: lc.score)
    reasoning = (
        f"Parlay ({len(scores)} legs). Weakest leg: {weakest.tier} ({weakest.score:.2f}). "
        f"Combined: {score:.2f} ({tier}). Source: {source_class} ceiling {ceiling}."
    )

    return EdgeConfidence(
        score=score, tier=tier, source_class=source_class,
        ceiling=ceiling, factors=factors, reasoning=reasoning,
    )
