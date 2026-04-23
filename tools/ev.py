"""
EV calculation engine — the only reason to bet is positive expected value.

Three modes:
  binary:    Moneylines and half-point spreads/totals (no push)
  with_push: Whole-number spreads/totals where push is possible
  free_bet:  Free bets return PROFIT ONLY (stake not returned)

Confidence-tiered edge thresholds prevent false positives.
"""

from tools.math_utils import american_to_decimal, american_to_implied


# ──────────────────────────────────────────────────
# EDGE THRESHOLDS (verified via false positive simulation)
# ──────────────────────────────────────────────────
# At +/-1.5 cent uncertainty on Pinnacle devigged line:
#   EV > 0% -> 10% false positive rate -> GARBAGE
#   EV > 2% -> 2.5% false positive rate -> ACCEPTABLE
#   EV > 3% -> 1.0% false positive rate -> GOOD

EDGE_THRESHOLDS = {
    "high": 0.02,      # Pinnacle devig: min EV 2%
    "medium": 0.03,    # Multi-book average: min EV 3%
    "low": 0.05,       # Our model only: min EV 5%
    "boost": 0.00,     # Any +EV boost is actionable
}


def ev_binary(fair_prob: float, decimal_odds: float) -> float:
    """
    For moneylines and half-point spreads/totals (no push possible).
    EV = (fair_prob x decimal_odds) - 1
    Verified: prob=0.55, odds=2.05 -> EV = 0.1275 (12.75%)
    """
    return (fair_prob * decimal_odds) - 1


def ev_with_push(p_win: float, p_push: float, decimal_odds: float) -> float:
    """
    For whole-number spreads/totals where push is possible.
    EV = p_win x (decimal - 1) - p_loss x 1
    where p_loss = 1 - p_win - p_push

    CRITICAL: Ignoring push halves the Kelly fraction. This matters.
    Verified: p_win=0.52, p_push=0.05, odds=1.909 -> EV=0.0427 (4.27%)
    """
    p_loss = 1 - p_win - p_push
    return (p_win * (decimal_odds - 1)) - (p_loss * 1.0)


def ev_free_bet(fair_prob: float, decimal_odds: float, free_bet_amount: float) -> float:
    """
    Free bets return PROFIT ONLY (stake not returned).
    EV = fair_prob x (decimal - 1) x amount
    No loss term (free bet costs you nothing).
    Verified: prob=0.27, odds=4.0, $100 -> EV=$81.00
    """
    return fair_prob * (decimal_odds - 1) * free_bet_amount


def evaluate_edge(
    fair_prob: float,
    book_odds_american: int,
    confidence: str = "medium",
    p_push: float = 0.0,
    stake: float = 100.0,
) -> dict:
    """
    Full edge evaluation: EV, edge, threshold check, recommendation.

    Args:
        fair_prob: Our estimated true probability
        book_odds_american: The line being offered
        confidence: 'high', 'medium', 'low', or 'boost'
        p_push: Push probability (0 for half-point lines)
        stake: Bet amount for dollar EV
    """
    decimal_odds = american_to_decimal(book_odds_american)
    book_implied = american_to_implied(book_odds_american)

    edge = fair_prob - book_implied

    if p_push > 0:
        ev_pct = ev_with_push(fair_prob, p_push, decimal_odds)
    else:
        ev_pct = ev_binary(fair_prob, decimal_odds)

    ev_dollar = ev_pct * stake
    min_ev = EDGE_THRESHOLDS.get(confidence, 0.03)
    actionable = ev_pct >= min_ev

    if ev_pct >= 0.07:
        rating = "STRONG"
    elif ev_pct >= 0.03:
        rating = "GOOD"
    elif ev_pct >= 0.01:
        rating = "MARGINAL"
    elif ev_pct > 0:
        rating = "THIN"
    else:
        rating = "NO_EDGE"

    return {
        "fair_prob": round(fair_prob, 4),
        "book_implied": round(book_implied, 4),
        "book_odds": book_odds_american,
        "decimal_odds": round(decimal_odds, 4),
        "edge": round(edge, 4),
        "edge_pct": round(edge * 100, 2),
        "ev": round(ev_pct, 4),
        "ev_pct": round(ev_pct * 100, 2),
        "ev_dollar": round(ev_dollar, 2),
        "p_push": p_push,
        "confidence": confidence,
        "min_ev_threshold": min_ev,
        "actionable": actionable,
        "rating": rating,
    }
