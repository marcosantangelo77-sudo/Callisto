"""Bet resolution (_resolve_line / resolve_line).

Extracted verbatim from tools/backtest_io.py.
"""

from typing import Optional

from tools.btio.teams import _team_matches


def resolve_line(
    market: str,
    side: str,
    line: Optional[float],
    home_score: int,
    away_score: int,
    home_team: str,
    away_team: str,
) -> Optional[str]:
    """Determine if a bet won, lost, or pushed."""
    total = home_score + away_score
    margin = home_score - away_score

    # Use fuzzy matching for side identification — side names from Odds API
    # may differ from game_results team names
    is_home = _team_matches(side, home_team)
    is_away = _team_matches(side, away_team)

    if market == "h2h":
        if is_home:
            return "won" if margin > 0 else "lost" if margin < 0 else "push"
        elif is_away:
            return "won" if margin < 0 else "lost" if margin > 0 else "push"
        return None

    if market == "spreads" and line is not None:
        # side is the team name, line is their spread
        if is_home:
            adjusted = margin + line
        else:
            adjusted = -margin + line

        if adjusted > 0:
            return "won"
        elif adjusted < 0:
            return "lost"
        return "push"

    if market == "totals" and line is not None:
        if side.lower() == "over":
            if total > line:
                return "won"
            elif total < line:
                return "lost"
            return "push"
        elif side.lower() == "under":
            if total < line:
                return "won"
            elif total > line:
                return "lost"
            return "push"

    return None
