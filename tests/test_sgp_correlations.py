"""Tests for tools.sgp_correlations."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from tools import sgp_correlations as sc


@pytest.fixture(autouse=True)
def _reset_cache():
    sc.reset_cache()
    yield
    sc.reset_cache()


def test_defaults_present_for_top_sports():
    tbl = sc.seed_from_defaults()
    # All four majors must have at least one pair
    for sport in ("nfl", "nba", "mlb", "nhl"):
        assert tbl.by_sport.get(sport), f"no defaults for {sport}"


def test_nfl_qb_wr_seeded_positive():
    rho = sc.get_correlation("nfl", "qb_pass_yds_over", "wr_rec_yds_over")
    assert 0.35 <= rho <= 0.6, f"unexpected rho={rho}"


def test_reverse_order_lookup():
    # Reverse order must hit the same cell
    forward = sc.get_correlation("nba", "player_pts_over", "team_total_over")
    reverse = sc.get_correlation("nba", "team_total_over", "player_pts_over")
    assert forward == reverse


def test_unknown_pair_falls_back_to_sport_prior():
    from tools.learned_correlations import sport_prior_fallback
    rho = sc.get_correlation("nfl", "this_doesnt_exist", "neither_does_this")
    assert rho == sport_prior_fallback("nfl")


def test_unknown_sport_returns_zero():
    rho = sc.get_correlation("cricket", "batter_hits_over", "team_total_over")
    assert rho == 0.0


def test_load_merges_yaml_override(tmp_path, monkeypatch):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sgp_correlations.yaml").write_text(
        "nfl:\n  qb_pass_yds_over|wr_rec_yds_over: 0.99\n",
        encoding="utf-8",
    )
    sc.reset_cache()
    tbl = sc.load(config_dir=cfg, force_reload=True)
    assert tbl.get("nfl", "qb_pass_yds_over", "wr_rec_yds_over") == pytest.approx(0.99)
    assert tbl.source("nfl", "qb_pass_yds_over", "wr_rec_yds_over") == "yaml_override"


def test_empirical_beats_override(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sgp_correlations.yaml").write_text(
        "nfl:\n  qb_pass_yds_over|wr_rec_yds_over: 0.50\n",
        encoding="utf-8",
    )
    (cfg / "sgp_correlations_empirical.yaml").write_text(
        "nfl:\n  qb_pass_yds_over|wr_rec_yds_over: 0.37\n",
        encoding="utf-8",
    )
    sc.reset_cache()
    tbl = sc.load(config_dir=cfg, force_reload=True)
    assert tbl.get("nfl", "qb_pass_yds_over", "wr_rec_yds_over") == pytest.approx(0.37)
    assert tbl.source("nfl", "qb_pass_yds_over", "wr_rec_yds_over") == "empirical"


def test_list_pairs_sorted_by_abs_rho():
    pairs = sc.list_pairs("nfl")
    assert pairs
    rhos = [abs(p[2]) for p in pairs]
    assert rhos == sorted(rhos, reverse=True)


def test_bad_yaml_values_dont_crash(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sgp_correlations.yaml").write_text(
        "nfl:\n  badkey_without_pipe: 0.5\n  broken|pair: notafloat\n",
        encoding="utf-8",
    )
    sc.reset_cache()
    tbl = sc.load(config_dir=cfg, force_reload=True)
    # No crash; the bad entries are silently skipped
    assert tbl.get("nfl", "broken", "pair") == 0.0


def test_clamp_out_of_range_yaml(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sgp_correlations.yaml").write_text(
        "nfl:\n  a|b: 5.0\n  c|d: -9.0\n",
        encoding="utf-8",
    )
    sc.reset_cache()
    tbl = sc.load(config_dir=cfg, force_reload=True)
    assert tbl.get("nfl", "a", "b") == 1.0
    assert tbl.get("nfl", "c", "d") == -1.0
