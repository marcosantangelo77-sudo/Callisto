"""Tests for the day-vs-night game classifier.

Previously the codebase (in places) used a ``commence_time_utc < 17:00``
heuristic which misclassified every West-Coast night game as a day game
(a 7pm PT start is 02:00Z, which is "before 17:00 UTC" but very obviously
a night game in any fan or sportsbook sense). The new classifier works
off the venue-local hour.
"""

from __future__ import annotations

import pytest

from tools.game_dates import is_day_game, local_hour_of_day


def test_1pm_local_is_day_game():
    # 1pm PT = 20:00 UTC
    assert is_day_game("2026-04-22T20:00:00Z", "baseball_mlb", "Los Angeles Dodgers") is True
    assert local_hour_of_day("2026-04-22T20:00:00Z", "baseball_mlb", "Los Angeles Dodgers") == 13


def test_7pm_local_is_night_game():
    # 7pm PT = 02:00Z next day
    assert is_day_game("2026-04-23T02:00:00Z", "baseball_mlb", "Los Angeles Dodgers") is False
    assert local_hour_of_day("2026-04-23T02:00:00Z", "baseball_mlb", "Los Angeles Dodgers") == 19


def test_boundary_17_local_is_night():
    # Exactly 5pm local → night (strict "<" threshold)
    # 17:00 PT = 00:00Z next day
    assert is_day_game("2026-04-23T00:00:00Z", "baseball_mlb", "Los Angeles Dodgers") is False


def test_1605_local_is_day():
    # 4:05pm PT (typical MLB getaway-day start) = 23:05Z same day
    assert is_day_game("2026-04-22T23:05:00Z", "baseball_mlb", "Los Angeles Dodgers") is True


def test_boston_1pm_et_is_day_game():
    # 1pm ET = 17:00 UTC
    assert is_day_game("2026-04-22T17:00:00Z", "baseball_mlb", "Boston Red Sox") is True


def test_boston_7pm_et_is_night_game():
    assert is_day_game("2026-04-22T23:00:00Z", "baseball_mlb", "Boston Red Sox") is False


def test_utc_17_for_west_coast_team_is_still_morning_local():
    """The regression: a game at 17:00 UTC is 10am PT — CLEARLY a day game
    in any fan sense. The old "< 17:00 UTC = day" heuristic only
    accidentally got this right because 10am is also before 5pm. The real
    failure mode was the inverse (see next test).
    """
    assert is_day_game("2026-04-22T17:00:00Z", "baseball_mlb", "San Francisco Giants") is True


def test_utc_01_for_west_coast_team_is_night_local():
    """The actual bug the old heuristic embodied: 01:00 UTC is LESS THAN
    17:00 UTC, so the old code said "day game". But 01:00 UTC is 6pm PT
    the previous day — a night game by any reasonable definition.
    """
    assert is_day_game("2026-04-23T01:00:00Z", "baseball_mlb", "San Francisco Giants") is False


def test_custom_threshold_hour():
    # Some sports (hockey?) might use a 6pm threshold
    # 5:30pm PT = 00:30Z next day
    commence = "2026-04-23T00:30:00Z"
    assert is_day_game(commence, "baseball_mlb", "Los Angeles Dodgers", threshold_hour=17) is False
    assert is_day_game(commence, "baseball_mlb", "Los Angeles Dodgers", threshold_hour=18) is True


def test_unparseable_returns_none():
    assert is_day_game("garbage", "baseball_mlb", "Boston Red Sox") is None
    assert local_hour_of_day("", "baseball_mlb", "Boston Red Sox") is None
