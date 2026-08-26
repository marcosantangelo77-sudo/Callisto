"""
Kelly Criterion sizing and best price selection for Callisto.

Kelly math:
  binary:    f* = (bp - q) / b
  with_push: f* = (b*p_win - p_loss) / b
  uncertainty-adjusted: scales by info_ratio = edge / noise

Best price: compare DK vs Fanatics, always take the better number.

DEPRECATION NOTE (feat/portfolio-kelly-live-loop, audit 2026-04-22):
    ``tools.kelly`` is the CANONICAL sizing module. It integrates AGP
    confidence tiers, variance dampening, portfolio Kelly with correlation,
    and ruin-probability modeling. The helpers in this file
    (``kelly_binary``, ``kelly_with_push``, ``uncertainty_adjusted_kelly``,
    ``bet_size``, ``bet_size_american``, ``best_price``) are retained as
    lightweight primitives and for the push-aware math that ``tools.kelly``
    doesn't cover — but any NEW caller doing single-bet or portfolio sizing
    should import from ``tools.kelly`` (``kelly_dynamic``, ``kelly_portfolio``).
"""

from tools.kelly import kelly_core
from tools.math_utils import american_to_decimal, american_to_implied
from tools.ev import ev_binary, ev_with_push


# Noise estimates by confidence level (in probability units)
NOISE = {
    "high": 0.015,    # +/-1.5 cents (Pinnacle devig)
    "medium": 0.025,  # +/-2.5 cents (multi-book average)
    "low": 0.040,     # +/-4.0 cents (our model alone)
}


def kelly_binary(fair_prob: float, decimal_odds: float) -> float:
    """
    f* = (bp - q) / b  where b = decimal-1, p = fair_prob, q = 1-p
    Returns 0 if bet is not +EV.
    Verified: prob=0.55, odds=2.10 -> f*=0.1409

    Thin wrapper: delegates to the canonical unrounded primitive
    ``tools.kelly.kelly_core`` so there is exactly one Kelly formula.
    Decimal odds -> net payout b; no rounding (unlike ``kelly_full``).
    """
    return kelly_core(float(fair_prob), float(decimal_odds) - 1.0)


def kelly_with_push(p_win: float, p_push: float, decimal_odds: float) -> float:
    """
    For spreads/totals at whole numbers.
    f* = (b*p_win - p_loss) / b  where p_loss = 1 - p_win - p_push

    CRITICAL: Ignoring push HALVES the Kelly fraction.
    Verified: p_win=0.54, p_push=0.04, odds=1.909 -> f*=0.078
    """
    b = decimal_odds - 1
    if b <= 0:
        return 0.0
    p_loss = 1 - p_win - p_push
    f = (b * p_win - p_loss) / b
    return max(f, 0)


def uncertainty_adjusted_kelly(
    kelly_fraction: float,
    edge_pct: float,
    confidence: str,
) -> float:
    """
    Scales Kelly for estimation uncertainty.

    info_ratio = edge / noise
      < 0.5: don't bet (edge within noise)
      0.5-1.0: scale x 0.3
      1.0-2.0: scale x 0.7
      > 2.0: scale x 1.0 (full quarter Kelly)

    Verified via Monte Carlo: +/-2 cent noise with full Kelly -> 4.5% ruin.
    Quarter Kelly with uncertainty adjustment -> 0% ruin.
    """
    noise = NOISE.get(confidence, 0.025)
    info_ratio = edge_pct / noise if noise > 0 else float("inf")

    if info_ratio < 0.5:
        scale = 0.0
    elif info_ratio < 1.0:
        scale = 0.3
    elif info_ratio < 2.0:
        scale = 0.7
    else:
        scale = 1.0

    return kelly_fraction * 0.25 * scale


def bet_size(
    bankroll: float,
    fair_prob: float,
    decimal_odds: float,
    confidence: str,
    max_wager: float = None,
    p_push: float = 0.0,
) -> dict:
    """
    Full sizing recommendation.
    Returns: recommended_stake, kelly_fraction, edge_pct, etc.
    """
    if p_push > 0:
        fk = kelly_with_push(fair_prob, p_push, decimal_odds)
        ev = ev_with_push(fair_prob, p_push, decimal_odds)
    else:
        fk = kelly_binary(fair_prob, decimal_odds)
        ev = ev_binary(fair_prob, decimal_odds)

    adjusted = uncertainty_adjusted_kelly(fk, ev, confidence)
    stake = bankroll * adjusted

    if max_wager:
        stake = min(stake, max_wager)

    return {
        "recommended_stake": round(stake, 2),
        "kelly_full": round(fk, 4),
        "kelly_quarter": round(fk * 0.25, 4),
        "kelly_adjusted": round(adjusted, 4),
        "edge_pct": round(ev * 100, 2),
        "confidence": confidence,
        "bankroll_risk_pct": round((stake / bankroll * 100), 2) if bankroll > 0 else 0,
        "max_capped": max_wager is not None and stake >= max_wager,
    }


def bet_size_american(
    bankroll: float,
    fair_prob: float,
    book_odds_american: int,
    confidence: str,
    max_wager: float = None,
    p_push: float = 0.0,
) -> dict:
    """Convenience wrapper that takes American odds."""
    return bet_size(
        bankroll=bankroll,
        fair_prob=fair_prob,
        decimal_odds=american_to_decimal(book_odds_american),
        confidence=confidence,
        max_wager=max_wager,
        p_push=p_push,
    )


# ──────────────────────────────────────────────────
# BEST PRICE SELECTOR
# ──────────────────────────────────────────────────

def best_price(dk_odds_american: int, fan_odds_american: int) -> dict:
    """
    For every bet, check BOTH DraftKings and Fanatics. Take the better price.
    Free edge. With 2 books, expected improvement = 0.5-1.5%.

    Returns which book has better price and by how much.
    """
    dk_dec = american_to_decimal(dk_odds_american)
    fan_dec = american_to_decimal(fan_odds_american)

    if dk_dec >= fan_dec:
        improvement = (dk_dec / fan_dec - 1) * 100 if fan_dec > 0 else 0
        return {
            "best_book": "draftkings",
            "best_odds_american": dk_odds_american,
            "best_decimal": round(dk_dec, 4),
            "other_book": "fanatics",
            "other_odds_american": fan_odds_american,
            "other_decimal": round(fan_dec, 4),
            "improvement_pct": round(improvement, 2),
        }
    else:
        improvement = (fan_dec / dk_dec - 1) * 100 if dk_dec > 0 else 0
        return {
            "best_book": "fanatics",
            "best_odds_american": fan_odds_american,
            "best_decimal": round(fan_dec, 4),
            "other_book": "draftkings",
            "other_odds_american": dk_odds_american,
            "other_decimal": round(dk_dec, 4),
            "improvement_pct": round(improvement, 2),
        }
