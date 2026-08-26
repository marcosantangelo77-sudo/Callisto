"""Notification message builders for the executor (slice 3 split).

Extracted from ``tools/bet_executor.py``: pure string builders for the
Telegram notifications. Sending stays at the call site (tools.telegram);
these helpers only format.
"""

from tools.betexec.config import MAX_DRAWDOWN_PCT


def build_bet_placed_message(
    *,
    game_description: str,
    team: str,
    side: str,
    odds: int,
    stake: float,
    edge: float,
    bankroll: float,
) -> str:
    """Format the BET PLACED Telegram body (legacy layout preserved)."""
    return (
        f"BET PLACED\n"
        f"{game_description or team}\n"
        f"{side} @ {'+' if odds > 0 else ''}{odds}\n"
        f"Stake: ${stake:.2f} | Edge: {edge*100:.1f}%\n"
        f"Bankroll: ${bankroll:.2f} → ${bankroll - stake:.2f}"
    )


__all__ = ["build_bet_placed_message", "MAX_DRAWDOWN_PCT"]
