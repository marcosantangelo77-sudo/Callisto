"""
Tests for the tools.environment -> tools.envimpact split.

Verifies that:
1. The facade re-exports every public name from the original module.
2. The submodules (venues, referees, weather, combined) are intact.
3. The model math is unchanged for representative inputs.
"""

import pytest

import tools.environment as facade
import tools.envimpact.combined as combined_mod
import tools.envimpact.referees as refs_mod
import tools.envimpact.venues as venues_mod
import tools.envimpact.weather as weather_mod


PUBLIC_NAMES = [
    "NFL_VENUES",
    "NBA_VENUES",
    "MLB_VENUES",
    "NBA_REFEREES",
    "MLB_UMPIRES",
    "NFL_REFEREES",
    "wind_impact",
    "temperature_impact",
    "altitude_impact",
    "humidity_impact",
    "get_venue_factors",
    "get_weather_adjustment",
    "ref_tendency",
    "total_environment_adjustment",
]


@pytest.mark.parametrize("name", PUBLIC_NAMES)
def test_facade_reexports_public_names(name):
    assert hasattr(facade, name)


def test_facade_names_are_submodule_objects():
    # The facade must not define its own copies — same objects as submodules
    assert facade.NFL_VENUES is venues_mod.NFL_VENUES
    assert facade.NBA_REFEREES is refs_mod.NBA_REFEREES
    assert facade.wind_impact is weather_mod.wind_impact
    assert facade.total_environment_adjustment is combined_mod.total_environment_adjustment


# ---------------------------------------------------------------------------
# Venue tables
# ---------------------------------------------------------------------------

def test_venue_table_sizes():
    assert len(venues_mod.NFL_VENUES) == 32  # 32 NFL teams
    assert len(venues_mod.NBA_VENUES) == 30  # 30 NBA teams
    assert len(venues_mod.MLB_VENUES) == 30  # 30 MLB teams


def test_venue_denver_altitude():
    assert venues_mod.NFL_VENUES["DEN"]["altitude_ft"] == 5280
    assert venues_mod.NBA_VENUES["DEN"]["altitude_ft"] == 5280
    assert venues_mod.MLB_VENUES["COL"]["altitude_ft"] == 5200


def test_venue_domes_have_no_wind_exposure():
    for code, v in venues_mod.NFL_VENUES.items():
        if v["dome"]:
            assert v["wind_exposure"] == 0, code


def test_park_factor_range():
    for code, v in venues_mod.MLB_VENUES.items():
        assert 0.5 < v["park_factor"] < 2.0, code
    assert venues_mod.MLB_VENUES["COL"]["park_factor"] == 1.380


# ---------------------------------------------------------------------------
# Wind model
# ---------------------------------------------------------------------------

def test_wind_calm_is_noop():
    r = weather_mod.wind_impact(5)
    assert r["total_adjustment"] == 0.0
    assert any("Minimal" in n for n in r["notes"])


def test_wind_moderate_suppresses_total():
    calm = weather_mod.wind_impact(5)["total_adjustment"]
    mod = weather_mod.wind_impact(18)["total_adjustment"]
    strong = weather_mod.wind_impact(27)["total_adjustment"]
    assert calm == 0.0
    assert mod < -1.0
    assert strong < mod


def test_wind_dome_venue_zeroes_out():
    r = weather_mod.wind_impact(30, venue="DET", sport="NFL")
    assert r["total_adjustment"] == 0.0
    assert "dome" in r["notes"][0].lower()


def test_wind_exposure_amplifies_chicago():
    chi = weather_mod.wind_impact(20, venue="CHI", sport="NFL")
    mia = weather_mod.wind_impact(20, venue="MIA", sport="NFL")
    assert abs(chi["total_adjustment"]) > abs(mia["total_adjustment"])


def test_mlb_wrigley_wind_out_amplified():
    wrigley = weather_mod.wind_impact(18, "out_to_CF", "CHC", "MLB")
    generic = weather_mod.wind_impact(18, "out_to_CF", None, "MLB")
    assert wrigley["total_adjustment"] > generic["total_adjustment"] > 0


def test_mlb_wrigley_wind_in_negative_and_amplified():
    wrigley = weather_mod.wind_impact(18, "in_from_CF", "CHC", "MLB")
    generic = weather_mod.wind_impact(18, "in_from_CF", None, "MLB")
    assert wrigley["total_adjustment"] < generic["total_adjustment"] < 0


def test_mlb_dome_wind_no_effect():
    r = weather_mod.wind_impact(25, "out_to_CF", "TB", "MLB")
    assert r["total_adjustment"] == 0.0
    assert "dome" in r["notes"][0].lower()


def test_generic_sport_wind_model():
    assert weather_mod.wind_impact(22, sport="MLS")["total_adjustment"] == -1.0
    assert weather_mod.wind_impact(16, sport="MLS")["total_adjustment"] == -0.5
    assert weather_mod.wind_impact(10, sport="MLS")["total_adjustment"] == 0.0


# ---------------------------------------------------------------------------
# Temperature model
# ---------------------------------------------------------------------------

def test_temperature_extreme_cold_nfl():
    r = weather_mod.temperature_impact(5, "NFL")
    assert r["total_adjustment"] <= -3.5


def test_temperature_monotone_cold_gradient():
    vals = [weather_mod.temperature_impact(t, "NFL")["total_adjustment"] for t in (5, 15, 25, 35, 45)]
    assert all(a < b for a, b in zip(vals, vals[1:]))
    assert vals[-1] == 0.0  # comfortable range


def test_temperature_heat_nfl():
    assert weather_mod.temperature_impact(95, "NFL")["total_adjustment"] < 0
    assert weather_mod.temperature_impact(75, "NFL")["total_adjustment"] == 0.0


def test_temperature_mlb_cold_under_hot_over():
    assert weather_mod.temperature_impact(40, "MLB")["total_adjustment"] < 0
    assert weather_mod.temperature_impact(90, "MLB")["total_adjustment"] > 0
    assert weather_mod.temperature_impact(70, "MLB")["total_adjustment"] == 0.0


# ---------------------------------------------------------------------------
# Altitude model
# ---------------------------------------------------------------------------

def test_altitude_coors_huge_positive():
    r = weather_mod.altitude_impact("COL", "MLB")
    assert r["altitude_ft"] == 5200
    assert r["total_adjustment"] > 3.0  # (1.38-1)*8.5 = 3.23


def test_altitude_nba_denver():
    r = weather_mod.altitude_impact("DEN", "NBA")
    assert r["total_adjustment"] == 3.5


def test_altitude_nfl_denver():
    r = weather_mod.altitude_impact("DEN", "NFL")
    assert r["total_adjustment"] == 1.5


def test_altitude_sea_level_neutral():
    r = weather_mod.altitude_impact("SF", "MLB")
    assert r["altitude_ft"] == 10
    # park factor 0.87 still applies even at sea level
    assert r["total_adjustment"] == round((0.87 - 1.0) * 8.5, 2)


def test_altitude_unknown_venue():
    r = weather_mod.altitude_impact("XXX", "NFL")
    assert r["altitude_ft"] == 0
    assert r["total_adjustment"] == 0.0
    assert not r["notes"][0].startswith("Low")


# ---------------------------------------------------------------------------
# Humidity model
# ---------------------------------------------------------------------------

def test_humidity_heat_stress_nfl_negative():
    assert weather_mod.humidity_impact(90, 95, "NFL") < 0


def test_humidity_mild_conditions_zero():
    assert weather_mod.humidity_impact(50, 70, "NFL") == 0.0


def test_humidity_dry_mlb_slight_over():
    assert weather_mod.humidity_impact(15, 80, "MLB") == 0.15


# ---------------------------------------------------------------------------
# Venue factors lookup
# ---------------------------------------------------------------------------

def test_get_venue_factors_nfl():
    f = weather_mod.get_venue_factors("GB", "NFL")
    assert f["venue_name"] == "Lambeau Field"
    assert f["surface"] == "grass"
    assert f["park_factor"] is None


def test_get_venue_factors_nba_indoor():
    f = weather_mod.get_venue_factors("DEN", "NBA")
    assert f["dome"] is True
    assert f["surface"] == "hardwood"


def test_get_venue_factors_mlb_park_factor():
    f = weather_mod.get_venue_factors("CHC", "MLB")
    assert f["park_factor"] == 1.060


def test_get_venue_factors_unknown():
    f = weather_mod.get_venue_factors("ZZZ", "NFL")
    assert "error" in f
    assert f["dome"] is False


# ---------------------------------------------------------------------------
# Combined weather adjustment
# ---------------------------------------------------------------------------

def test_weather_adjustment_dome_short_circuits_weather():
    r = weather_mod.get_weather_adjustment(
        "NO", "NFL", {"wind_speed_mph": 30, "temp_f": 20, "precipitation": "snow"}
    )
    factors = {f["factor"] for f in r["factors"]}
    assert "wind" not in factors and "temperature" not in factors
    assert "dome" in factors


def test_weather_adjustment_no_data_venue_only():
    r = weather_mod.get_weather_adjustment("DEN", "NBA")
    assert r["total_adj"] == 3.5  # altitude only


def test_weather_adjustment_full_blizzard_buffalo():
    r = weather_mod.get_weather_adjustment(
        "BUF", "NFL",
        {"wind_speed_mph": 28, "temp_f": 12, "humidity_pct": 60,
         "precipitation": "snow", "wind_direction": "W"},
    )
    factor_names = [f["factor"] for f in r["factors"]]
    assert {"wind", "temperature", "precipitation"} <= set(factor_names)
    assert r["total_adj"] < -6.0  # heavy suppression


# ---------------------------------------------------------------------------
# Referee tendencies
# ---------------------------------------------------------------------------

def test_ref_lookup_exact_match():
    rt = combined_mod.ref_tendency("Scott Foster", "NBA")
    assert rt["found"] and rt["total_adjustment"] == -2.0


def test_ref_lookup_case_insensitive_partial():
    rt = combined_mod.ref_tendency("  foster ", "NBA")
    assert rt["found"] and rt["ref_name"] == "Scott Foster"


def test_ref_lookup_not_found_neutral():
    rt = combined_mod.ref_tendency("Nobody Important", "NBA")
    assert rt["found"] is False
    assert rt["total_adjustment"] == 0.0


def test_ref_lookup_unsupported_sport():
    rt = combined_mod.ref_tendency("Anyone", "NHL")
    assert rt["found"] is False
    assert "NHL" in rt["notes"]


def test_umpire_lookup_fields():
    rt = combined_mod.ref_tendency("Joe West", "MLB")
    assert rt["found"]
    assert rt["zone_size_delta"] == 0.12
    assert rt["k_rate_impact"] == 0.05
    assert rt["total_adjustment"] == -0.6


def test_nfl_ref_lookup_pi_rate():
    rt = combined_mod.ref_tendency("Shawn Hochuli", "NFL")
    assert rt["pass_interference_rate"] == 1.20
    assert rt["foul_rate_impact"] == 0.12


# ---------------------------------------------------------------------------
# Total environment adjustment (main entry point)
# ---------------------------------------------------------------------------

def test_total_adjustment_negligible_neutral_lean():
    r = combined_mod.total_environment_adjustment("DAL", "NFL")
    assert r["significance"] == "negligible"
    assert r["lean"] == "NEUTRAL"
    assert r["confidence"] >= 0.3


def test_total_adjustment_extreme_conditions():
    r = combined_mod.total_environment_adjustment(
        "BUF", "NFL",
        weather={"wind_speed_mph": 28, "temp_f": 8, "precipitation": "snow"},
        refs=["Brad Allen"],
    )
    assert r["lean"] == "UNDER"
    assert r["significance"] in ("significant", "extreme")
    assert r["confidence"] > 0.7
    factor_names = {f["factor"] for f in r["factors_breakdown"]}
    assert {"wind", "temperature", "referees"} <= factor_names


def test_total_adjustment_coors_over_lean():
    r = combined_mod.total_environment_adjustment("COL", "MLB", refs=["Pat Hoberg"])
    assert r["lean"] == "OVER"
    assert r["total_adj"] > 2.5


def test_total_adjustment_confidence_caps_at_one():
    r = combined_mod.total_environment_adjustment(
        "BUF", "NFL",
        weather={"wind_speed_mph": 25, "temp_f": 10, "humidity_pct": 40, "precipitation": "rain"},
        refs=["Carl Cheffers"],
    )
    assert r["confidence"] <= 1.0


def test_summary_mentions_venue_and_refs():
    s = combined_mod._build_summary(
        "CHI", "NFL", -4.2, "significant", "UNDER",
        {"wind_speed_mph": 20}, ["Craig Wrolstad"],
    )
    assert "CHI" in s and "UNDER" in s and "Wrolstad" in s and "Wind 20 mph" in s
