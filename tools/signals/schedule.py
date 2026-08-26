"""Schedule/date helpers extracted from tools.backtest (god-module diet).

Owns the venue-local game-date derivation used by
``BacktestEngine.generate_paper_trade_signal``.
"""

from __future__ import annotations

import datetime


def game_date_from_commence(
    game_obj: dict,
    sport: str = "",
    today: str | None = None,
) -> str:
    """Venue-local game date for this game.

    Pre-fix this sliced ``commence_time[:10]`` which is the UTC date.
    For a Dodgers 7:30pm PT home game (``02:30Z`` next day) that
    returned tomorrow's UTC date, causing silent day-of-week and
    day/night cohort corruption. Now: convert to the venue's local
    timezone via ``tools.game_dates.local_game_date``.
    """
    from tools.game_dates import local_game_date as _lgd

    if today is None:
        today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")

    ct = game_obj.get("commence_time", "")
    if not ct:
        return today
    home = game_obj.get("home_team", "")
    sp = game_obj.get("sport_key") or sport or ""
    d = _lgd(ct, sp, home)
    if d is not None:
        return d.isoformat()
    # Fallback to pre-existing UTC-slice behavior only if the helper
    # couldn't parse the timestamp — better than inventing a date.
    if len(ct) >= 10:
        return ct[:10]
    return today
