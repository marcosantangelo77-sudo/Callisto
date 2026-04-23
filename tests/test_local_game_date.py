"""Tests for the canonical venue-local game-date helper.

The key regression these tests guard against: a Dodgers 7:30pm PT home game
with commence_time ``2026-04-22T02:30:00Z`` must resolve to
``local_game_date=2026-04-21`` — not ``2026-04-22`` (UTC-sliced) and not
whatever the process's system clock's timezone happens to be.
"""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from tools.game_dates import (
    get_venue_timezone,
    local_day_of_week,
    local_game_date,
    local_hour_of_day,
)


# ─────────────────────────────────────────────
# The load-bearing regression case
# ─────────────────────────────────────────────

def test_dodgers_west_coast_night_game_stays_on_local_date():
    """The canonical bug case: Dodgers 7:30pm PT home game spills into next
    UTC day. Before the fix this showed up as ``2026-04-22`` (UTC-sliced)
    which was a Wednesday, when it should have been Tuesday April 21 PT.
    """
    commence = "2026-04-22T02:30:00Z"  # April 21 7:30pm PT
    d = local_game_date(commence, "baseball_mlb", "Los Angeles Dodgers")
    assert d == date(2026, 4, 21)
    assert local_hour_of_day(commence, "baseball_mlb", "Los Angeles Dodgers") == 19
    # Tuesday = 1 on Python's Monday=0 convention
    assert local_day_of_week(commence, "baseball_mlb", "Los Angeles Dodgers") == 1


def test_boston_eastern_night_game_has_utc_and_local_agree():
    """Boston 7:10pm ET is 11:10pm UTC same day — UTC and local date agree
    already, no shift needed. The test confirms we don't accidentally
    off-by-one the East Coast while fixing the West.
    """
    commence = "2026-04-22T23:10:00Z"  # April 22 7:10pm ET
    d = local_game_date(commence, "baseball_mlb", "Boston Red Sox")
    assert d == date(2026, 4, 22)
    assert local_hour_of_day(commence, "baseball_mlb", "Boston Red Sox") == 19


def test_giants_late_night_game_crosses_utc_midnight():
    commence = "2026-07-04T03:45:00Z"  # July 3 8:45pm PT
    d = local_game_date(commence, "baseball_mlb", "San Francisco Giants")
    assert d == date(2026, 7, 3)


def test_rockies_mountain_time_edge_case():
    commence = "2026-06-15T01:15:00Z"  # June 14 7:15pm MT (DST, UTC-6)
    d = local_game_date(commence, "baseball_mlb", "Colorado Rockies")
    assert d == date(2026, 6, 14)


def test_diamondbacks_phoenix_no_dst():
    """Arizona doesn't observe DST; confirm America/Phoenix mapping."""
    commence = "2026-07-10T02:40:00Z"  # July 9 7:40pm AZ (UTC-7 year-round)
    d = local_game_date(commence, "baseball_mlb", "Arizona Diamondbacks")
    assert d == date(2026, 7, 9)


# ─────────────────────────────────────────────
# Unknown venue fallback
# ─────────────────────────────────────────────

def test_unknown_team_falls_back_to_sport_default():
    # Sport default for MLB is America/New_York
    commence = "2026-04-22T23:10:00Z"
    d = local_game_date(commence, "baseball_mlb", "Buffalo Bisons Triple-A")
    # Would be 7:10pm ET → April 22
    assert d == date(2026, 4, 22)


def test_unknown_sport_still_returns_valid_date():
    """No crash, no exception — returns SOMETHING rather than failing open."""
    d = local_game_date("2026-04-22T18:00:00Z", "kabaddi_mystery", "Some Team")
    assert d is not None
    assert isinstance(d, date)


def test_unknown_everything_uses_america_new_york():
    # 18:00 UTC → 14:00 ET — same calendar day
    d = local_game_date("2026-04-22T18:00:00Z", "", "")
    assert d == date(2026, 4, 22)


# ─────────────────────────────────────────────
# Input format tolerance
# ─────────────────────────────────────────────

def test_accepts_trailing_z_and_offset_forms():
    a = local_game_date("2026-04-22T02:30:00Z", "baseball_mlb", "Los Angeles Dodgers")
    b = local_game_date("2026-04-22T02:30:00+00:00", "baseball_mlb", "Los Angeles Dodgers")
    assert a == b == date(2026, 4, 21)


def test_accepts_fractional_seconds():
    d = local_game_date(
        "2026-04-22T02:30:15.456Z", "baseball_mlb", "Los Angeles Dodgers"
    )
    assert d == date(2026, 4, 21)


def test_accepts_naive_datetime_as_utc():
    naive = datetime(2026, 4, 22, 2, 30, 0)  # no tzinfo
    d = local_game_date(naive, "baseball_mlb", "Los Angeles Dodgers")
    assert d == date(2026, 4, 21)


def test_accepts_aware_datetime():
    aware = datetime(2026, 4, 22, 2, 30, 0, tzinfo=timezone.utc)
    d = local_game_date(aware, "baseball_mlb", "Los Angeles Dodgers")
    assert d == date(2026, 4, 21)


def test_empty_commence_returns_none():
    assert local_game_date("", "baseball_mlb", "Los Angeles Dodgers") is None
    assert local_game_date(None, "baseball_mlb", "Los Angeles Dodgers") is None


def test_unparseable_commence_returns_none():
    assert local_game_date("not-a-date", "baseball_mlb", "Boston Red Sox") is None


# ─────────────────────────────────────────────
# Timezone object lookup
# ─────────────────────────────────────────────

def test_get_venue_timezone_maps_known_west_coast_teams():
    tz = get_venue_timezone("baseball_mlb", "Los Angeles Dodgers")
    assert str(tz) == "America/Los_Angeles"


def test_get_venue_timezone_maps_nhl_team():
    tz = get_venue_timezone("icehockey_nhl", "Vegas Golden Knights")
    assert str(tz) == "America/Los_Angeles"


def test_get_venue_timezone_unknown_falls_back_to_default():
    tz = get_venue_timezone("baseball_mlb", "UnknownTeam")
    assert str(tz) == "America/New_York"


def test_get_venue_timezone_caches_same_instance():
    # Cache isn't strict-identity required, but same name → same object is
    # the whole point of the cache.
    tz1 = get_venue_timezone("baseball_mlb", "Los Angeles Dodgers")
    tz2 = get_venue_timezone("baseball_mlb", "Los Angeles Dodgers")
    assert tz1 is tz2


# ─────────────────────────────────────────────
# DST boundary
# ─────────────────────────────────────────────

def test_dst_spring_forward_handled_correctly():
    """March 9 2025 2am → 3am in US. A commence_time at 2:30am UTC on
    March 10 2025 is 10:30pm ET March 9 (EST, UTC-5) — before DST the
    Monday before. Make sure the math is tz-aware."""
    # March 9 2025 @ 10:30pm ET = March 10 02:30 UTC (still EST, UTC-5)
    # Wait — DST started March 9 2025 at 2am ET. So at 10:30pm ET March 9
    # the zone is already EDT (UTC-4), so UTC is March 10 02:30.
    commence = "2025-03-10T02:30:00Z"
    d = local_game_date(commence, "baseball_mlb", "Boston Red Sox")
    assert d == date(2025, 3, 9)
