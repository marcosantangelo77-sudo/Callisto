"""Canonical timezone-aware game-date helpers.

The historical bug: every table in Callisto that stored a "game date" used a
different timezone. ``game_results.game_date`` came from ESPN's ET-oriented
scoreboard date; ``backtest_events.game_date`` was ``commence_time[:10]``
extracted directly from the UTC ISO timestamp. A Dodgers 7:30pm PT home game
on April 21 would have ``commence_time = 2026-04-22T02:30:00Z`` — so one
table tagged it ``2026-04-21`` (ET rollover) and the other ``2026-04-22``
(UTC rollover). Day-of-week and day/night cohort analysis was silently
corrupted for every West-Coast late game.

This module introduces a single canonical date: the DATE in the **venue's
local timezone** at ``commence_time``. All new code paths compute this via
``local_game_date()`` and persist it to ``local_game_date`` columns. The
legacy ``game_date`` columns remain for backward compatibility but are
deprecated.

Functions here are:
  - deterministic (no dependence on the process's local tz)
  - pure (no DB lookups; VENUE_METADATA is an in-memory dict)
  - cheap (hot-path safe: a single ZoneInfo construction + ``.astimezone()``)
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional, Union
from zoneinfo import ZoneInfo


# ─────────────────────────────────────────────
# Team → IANA timezone mappings
# ─────────────────────────────────────────────
# Source-of-truth mapping from home-team display name → IANA timezone.
# VENUE_METADATA in data_collector.py uses venue names; this mirror uses TEAM
# names because backtest/paper-trade rows store home_team, not venue_name.
#
# When a team isn't listed, we fall back to the sport default (see
# SPORT_DEFAULT_TZ below). That default is America/New_York for every US
# major-league sport — matches how sportsbooks and ESPN publish schedules
# "for April 21" even when a West-Coast game crosses UTC midnight.
MLB_TEAM_TZ: dict[str, str] = {
    # AL East
    "Baltimore Orioles": "America/New_York",
    "Boston Red Sox": "America/New_York",
    "New York Yankees": "America/New_York",
    "Tampa Bay Rays": "America/New_York",
    "Toronto Blue Jays": "America/Toronto",
    # AL Central
    "Chicago White Sox": "America/Chicago",
    "Cleveland Guardians": "America/New_York",
    "Detroit Tigers": "America/Detroit",
    "Kansas City Royals": "America/Chicago",
    "Minnesota Twins": "America/Chicago",
    # AL West
    "Houston Astros": "America/Chicago",
    "Los Angeles Angels": "America/Los_Angeles",
    "Oakland Athletics": "America/Los_Angeles",
    "Athletics": "America/Los_Angeles",  # post-Sacramento label
    "Seattle Mariners": "America/Los_Angeles",
    "Texas Rangers": "America/Chicago",
    # NL East
    "Atlanta Braves": "America/New_York",
    "Miami Marlins": "America/New_York",
    "New York Mets": "America/New_York",
    "Philadelphia Phillies": "America/New_York",
    "Washington Nationals": "America/New_York",
    # NL Central
    "Chicago Cubs": "America/Chicago",
    "Cincinnati Reds": "America/New_York",
    "Milwaukee Brewers": "America/Chicago",
    "Pittsburgh Pirates": "America/New_York",
    "St. Louis Cardinals": "America/Chicago",
    # NL West
    "Arizona Diamondbacks": "America/Phoenix",
    "Colorado Rockies": "America/Denver",
    "Los Angeles Dodgers": "America/Los_Angeles",
    "San Diego Padres": "America/Los_Angeles",
    "San Francisco Giants": "America/Los_Angeles",
}

NBA_TEAM_TZ: dict[str, str] = {
    "Atlanta Hawks": "America/New_York",
    "Boston Celtics": "America/New_York",
    "Brooklyn Nets": "America/New_York",
    "Charlotte Hornets": "America/New_York",
    "Chicago Bulls": "America/Chicago",
    "Cleveland Cavaliers": "America/New_York",
    "Dallas Mavericks": "America/Chicago",
    "Denver Nuggets": "America/Denver",
    "Detroit Pistons": "America/Detroit",
    "Golden State Warriors": "America/Los_Angeles",
    "Houston Rockets": "America/Chicago",
    "Indiana Pacers": "America/Indiana/Indianapolis",
    "LA Clippers": "America/Los_Angeles",
    "Los Angeles Clippers": "America/Los_Angeles",
    "Los Angeles Lakers": "America/Los_Angeles",
    "Memphis Grizzlies": "America/Chicago",
    "Miami Heat": "America/New_York",
    "Milwaukee Bucks": "America/Chicago",
    "Minnesota Timberwolves": "America/Chicago",
    "New Orleans Pelicans": "America/Chicago",
    "New York Knicks": "America/New_York",
    "Oklahoma City Thunder": "America/Chicago",
    "Orlando Magic": "America/New_York",
    "Philadelphia 76ers": "America/New_York",
    "Phoenix Suns": "America/Phoenix",
    "Portland Trail Blazers": "America/Los_Angeles",
    "Sacramento Kings": "America/Los_Angeles",
    "San Antonio Spurs": "America/Chicago",
    "Toronto Raptors": "America/Toronto",
    "Utah Jazz": "America/Denver",
    "Washington Wizards": "America/New_York",
}

NHL_TEAM_TZ: dict[str, str] = {
    "Anaheim Ducks": "America/Los_Angeles",
    "Arizona Coyotes": "America/Phoenix",
    "Boston Bruins": "America/New_York",
    "Buffalo Sabres": "America/New_York",
    "Calgary Flames": "America/Edmonton",
    "Carolina Hurricanes": "America/New_York",
    "Chicago Blackhawks": "America/Chicago",
    "Colorado Avalanche": "America/Denver",
    "Columbus Blue Jackets": "America/New_York",
    "Dallas Stars": "America/Chicago",
    "Detroit Red Wings": "America/Detroit",
    "Edmonton Oilers": "America/Edmonton",
    "Florida Panthers": "America/New_York",
    "Los Angeles Kings": "America/Los_Angeles",
    "Minnesota Wild": "America/Chicago",
    "Montreal Canadiens": "America/Montreal",
    "Montréal Canadiens": "America/Montreal",
    "Nashville Predators": "America/Chicago",
    "New Jersey Devils": "America/New_York",
    "New York Islanders": "America/New_York",
    "New York Rangers": "America/New_York",
    "Ottawa Senators": "America/Toronto",
    "Philadelphia Flyers": "America/New_York",
    "Pittsburgh Penguins": "America/New_York",
    "San Jose Sharks": "America/Los_Angeles",
    "Seattle Kraken": "America/Los_Angeles",
    "St. Louis Blues": "America/Chicago",
    "Tampa Bay Lightning": "America/New_York",
    "Toronto Maple Leafs": "America/Toronto",
    "Utah Hockey Club": "America/Denver",
    "Vancouver Canucks": "America/Vancouver",
    "Vegas Golden Knights": "America/Los_Angeles",
    "Washington Capitals": "America/New_York",
    "Winnipeg Jets": "America/Winnipeg",
}

NFL_TEAM_TZ: dict[str, str] = {
    "Arizona Cardinals": "America/Phoenix",
    "Atlanta Falcons": "America/New_York",
    "Baltimore Ravens": "America/New_York",
    "Buffalo Bills": "America/New_York",
    "Carolina Panthers": "America/New_York",
    "Chicago Bears": "America/Chicago",
    "Cincinnati Bengals": "America/New_York",
    "Cleveland Browns": "America/New_York",
    "Dallas Cowboys": "America/Chicago",
    "Denver Broncos": "America/Denver",
    "Detroit Lions": "America/Detroit",
    "Green Bay Packers": "America/Chicago",
    "Houston Texans": "America/Chicago",
    "Indianapolis Colts": "America/Indiana/Indianapolis",
    "Jacksonville Jaguars": "America/New_York",
    "Kansas City Chiefs": "America/Chicago",
    "Las Vegas Raiders": "America/Los_Angeles",
    "Los Angeles Chargers": "America/Los_Angeles",
    "Los Angeles Rams": "America/Los_Angeles",
    "Miami Dolphins": "America/New_York",
    "Minnesota Vikings": "America/Chicago",
    "New England Patriots": "America/New_York",
    "New Orleans Saints": "America/Chicago",
    "New York Giants": "America/New_York",
    "New York Jets": "America/New_York",
    "Philadelphia Eagles": "America/New_York",
    "Pittsburgh Steelers": "America/New_York",
    "San Francisco 49ers": "America/Los_Angeles",
    "Seattle Seahawks": "America/Los_Angeles",
    "Tampa Bay Buccaneers": "America/New_York",
    "Tennessee Titans": "America/Chicago",
    "Washington Commanders": "America/New_York",
}

SPORT_TEAM_TZ: dict[str, dict[str, str]] = {
    "baseball_mlb": MLB_TEAM_TZ,
    "basketball_nba": NBA_TEAM_TZ,
    "icehockey_nhl": NHL_TEAM_TZ,
    "americanfootball_nfl": NFL_TEAM_TZ,
}

SPORT_DEFAULT_TZ: dict[str, str] = {
    "baseball_mlb": "America/New_York",
    "basketball_nba": "America/New_York",
    "basketball_ncaab": "America/New_York",
    "basketball_ncaaw": "America/New_York",
    "basketball_wnba": "America/New_York",
    "icehockey_nhl": "America/New_York",
    "americanfootball_nfl": "America/New_York",
    "americanfootball_ncaaf": "America/New_York",
    "golf_pga": "America/New_York",
    "soccer_epl": "Europe/London",
    "soccer_mls": "America/New_York",
}

_DEFAULT_TZ = "America/New_York"

# Cache ZoneInfo objects — they're cheap but constructing them thousands of
# times per backtest is measurable.
_tz_cache: dict[str, ZoneInfo] = {}


def _tz(name: str) -> ZoneInfo:
    z = _tz_cache.get(name)
    if z is None:
        z = ZoneInfo(name)
        _tz_cache[name] = z
    return z


def get_venue_timezone(sport: str, home_team: Optional[str]) -> ZoneInfo:
    """Return the IANA timezone for the venue where this game is played.

    Precedence:
      1. Exact team lookup in the sport's team→tz map
      2. Sport default (usually America/New_York)
      3. America/New_York as a final fallback

    Never raises. ``home_team`` of ``None`` or unknown falls through to the
    sport default, matching how sportsbooks publish ET-dated schedules for
    games whose venue we don't recognize yet.
    """
    sport = (sport or "").strip()
    home_team = (home_team or "").strip()

    team_map = SPORT_TEAM_TZ.get(sport, {})
    if home_team and home_team in team_map:
        return _tz(team_map[home_team])

    default = SPORT_DEFAULT_TZ.get(sport, _DEFAULT_TZ)
    return _tz(default)


def _parse_commence(commence_time_utc: Union[str, datetime]) -> Optional[datetime]:
    """Parse an ISO-8601 UTC commence time to a tz-aware ``datetime``.

    Accepts either a ``datetime`` (assumed UTC if naive) or a string in one of
    the shapes the Odds API emits:
      - ``2026-04-22T02:30:00Z``
      - ``2026-04-22T02:30:00+00:00``
      - ``2026-04-22T02:30:00.123Z``

    Returns ``None`` if unparseable — callers MUST handle that (we don't want
    to silently coerce a bad value to "now" and repeat the original bug).
    """
    if isinstance(commence_time_utc, datetime):
        dt = commence_time_utc
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    if not isinstance(commence_time_utc, str) or not commence_time_utc:
        return None

    s = commence_time_utc.strip()
    # datetime.fromisoformat doesn't accept trailing Z in Python <3.11
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        # Last-ditch: try just the YYYY-MM-DDTHH:MM:SS prefix
        if len(s) >= 19:
            try:
                dt = datetime.fromisoformat(s[:19]).replace(tzinfo=timezone.utc)
            except ValueError:
                return None
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def local_game_date(
    commence_time_utc: Union[str, datetime],
    sport: str,
    home_team: Optional[str],
) -> Optional[date]:
    """The DATE of this game in the venue's local timezone.

    This is Callisto's canonical "game date" — the date a fan, a sportsbook,
    and ESPN would all agree the game was played.

    Returns ``None`` if ``commence_time_utc`` cannot be parsed. Migrations
    must treat ``None`` as "skip row, leave NULL"; the migration reports
    count of un-normalizable rows separately.

    Example:
      >>> local_game_date(
      ...     "2026-04-22T02:30:00Z",
      ...     "baseball_mlb",
      ...     "Los Angeles Dodgers",
      ... )
      datetime.date(2026, 4, 21)
    """
    dt = _parse_commence(commence_time_utc)
    if dt is None:
        return None
    tz = get_venue_timezone(sport, home_team)
    return dt.astimezone(tz).date()


def local_hour_of_day(
    commence_time_utc: Union[str, datetime],
    sport: str,
    home_team: Optional[str],
) -> Optional[int]:
    """Local hour (0-23) of the first pitch / tip / puck-drop.

    Used by the day-vs-night classifier: ``local_hour_of_day(...) < 17`` is
    the conventional threshold for a "day game" in MLB fan/betting-market
    usage (1pm-4pm starts classified as day; 5pm+ as night).
    """
    dt = _parse_commence(commence_time_utc)
    if dt is None:
        return None
    tz = get_venue_timezone(sport, home_team)
    return dt.astimezone(tz).hour


def local_day_of_week(
    commence_time_utc: Union[str, datetime],
    sport: str,
    home_team: Optional[str],
) -> Optional[int]:
    """ISO day-of-week (Monday=0 .. Sunday=6) in venue-local time.

    Uses ``datetime.weekday()``, which returns Monday=0 by convention. This
    matches polars' ``dt.weekday()`` return shape used in temporal_analysis.
    """
    dt = _parse_commence(commence_time_utc)
    if dt is None:
        return None
    tz = get_venue_timezone(sport, home_team)
    return dt.astimezone(tz).weekday()


def is_day_game(
    commence_time_utc: Union[str, datetime],
    sport: str,
    home_team: Optional[str],
    threshold_hour: int = 17,
) -> Optional[bool]:
    """Classify a game as "day" or "night" in the fan-perception sense.

    Defaults to a 5pm-local cutoff: anything starting before 17:00 local
    counts as a day game. This matches MLB broadcast-market conventions and
    the way sportsbook day-game promotions are defined.

    Returns ``None`` if the commence time can't be parsed — caller should
    not treat that as either True or False.
    """
    hour = local_hour_of_day(commence_time_utc, sport, home_team)
    if hour is None:
        return None
    return hour < threshold_hour


__all__ = [
    "get_venue_timezone",
    "local_game_date",
    "local_hour_of_day",
    "local_day_of_week",
    "is_day_game",
    "MLB_TEAM_TZ",
    "NBA_TEAM_TZ",
    "NHL_TEAM_TZ",
    "NFL_TEAM_TZ",
    "SPORT_TEAM_TZ",
    "SPORT_DEFAULT_TZ",
]
