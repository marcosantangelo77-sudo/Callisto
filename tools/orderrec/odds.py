"""American-odds math helpers (split from ``tools/order_reconciler``)."""

from __future__ import annotations

from typing import Optional


def _american_pnl(stake: float, price_american: int, result: str) -> float:
    """PnL in dollars from an American-odds wager.

    Win: stake * (odds - 1) in decimal terms.
    Loss: -stake.
    Push: 0.
    """
    if not stake or not price_american:
        return 0.0
    if result == "push":
        return 0.0
    if result == "loss":
        return -float(stake)
    # win
    p = int(price_american)
    if p > 0:
        return float(stake) * (p / 100.0)
    return float(stake) * (100.0 / abs(p))


def _american_payout(stake: float, price_american: int) -> float:
    """Stake + profit for winning American odds (for bets.payout mirror)."""
    if not stake:
        return 0.0
    p = int(price_american)
    if p > 0:
        return float(stake) * (1 + p / 100.0)
    return float(stake) * (1 + 100.0 / abs(p))


def _american_to_implied(price_american: int) -> Optional[float]:
    """Implied probability from American odds. None if price is falsy."""
    if not price_american:
        return None
    p = int(price_american)
    if p > 0:
        return 100.0 / (p + 100.0)
    return abs(p) / (abs(p) + 100.0)


def _team_matches(side: str, team: str) -> bool:
    """Loose team-name match. game_results stores full names; orders store
    whatever the signal emitter wrote (often a short code or full name).
    """
    if not side or not team:
        return False
    s = side.lower().strip()
    t = team.lower().strip()
    return s == t or s in t or t in s
