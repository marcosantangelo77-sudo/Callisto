"""Tests for tools.sgp_scanner."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import pytest

from tools import sgp_correlations as sc
from tools.sgp_scanner import (
    SGPEdge,
    SGPLeg,
    _american_to_implied,
    _implied_to_american,
    enumerate_candidates,
    legs_from_game_odds,
    scan_sgp_edges,
    synthesize_book_sgp_price,
    theoretical_sgp_prob,
)


@pytest.fixture(autouse=True)
def _fresh_table(tmp_path, monkeypatch):
    # Use an isolated config dir so tests don't pick up calibrated YAMLs
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    monkeypatch.setenv("CALLISTO_CONFIG_DIR", str(cfg))
    sc.reset_cache()
    # Force load against the isolated dir so the override env is actually used
    sc.load(config_dir=cfg, force_reload=True)
    yield
    sc.reset_cache()


# ---------------------------------------------------------------------------
# Math identity tests
# ---------------------------------------------------------------------------

def test_uncorrelated_legs_give_independent_product(tmp_path):
    """rho=0 -> joint = product of legs."""
    # Install an override that says rho=0 for the pair we'll use
    cfg = Path(tmp_path) / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "sgp_correlations.yaml").write_text(
        "nfl:\n  fake_a_over|fake_b_over: 0.00\n",
        encoding="utf-8",
    )
    sc.reset_cache()
    sc.load(config_dir=cfg, force_reload=True)

    legs = [
        SGPLeg(leg_type="fake_a_over", description="a", american_odds=-110, fair_prob=0.55),
        SGPLeg(leg_type="fake_b_over", description="b", american_odds=-110, fair_prob=0.60),
    ]
    theo, naive, pair_info = theoretical_sgp_prob("nfl", legs)
    assert naive == pytest.approx(0.55 * 0.60, abs=1e-9)
    assert theo == pytest.approx(0.55 * 0.60, abs=1e-3)
    assert pair_info[0]["rho"] == 0.0


def test_fully_correlated_legs_give_min_prob(tmp_path):
    """rho=1 -> joint = min(P_A, P_B)."""
    cfg = Path(tmp_path) / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "sgp_correlations.yaml").write_text(
        "nfl:\n  fc_a_over|fc_b_over: 1.00\n",
        encoding="utf-8",
    )
    sc.reset_cache()
    sc.load(config_dir=cfg, force_reload=True)

    legs = [
        SGPLeg(leg_type="fc_a_over", description="a", american_odds=-110, fair_prob=0.55),
        SGPLeg(leg_type="fc_b_over", description="b", american_odds=-110, fair_prob=0.60),
    ]
    theo, naive, _ = theoretical_sgp_prob("nfl", legs)
    # Bivariate copula with rho=1 collapses to min(p_a, p_b)
    assert theo == pytest.approx(min(0.55, 0.60), abs=1e-3)
    assert theo > naive


def test_positive_correlation_raises_joint(tmp_path):
    """rho>0 -> joint > independent product."""
    cfg = Path(tmp_path) / "cfg"
    cfg.mkdir(exist_ok=True)
    (cfg / "sgp_correlations.yaml").write_text(
        "nfl:\n  pc_a_over|pc_b_over: 0.40\n",
        encoding="utf-8",
    )
    sc.reset_cache()
    sc.load(config_dir=cfg, force_reload=True)

    legs = [
        SGPLeg(leg_type="pc_a_over", description="a", american_odds=-110, fair_prob=0.55),
        SGPLeg(leg_type="pc_b_over", description="b", american_odds=-110, fair_prob=0.60),
    ]
    theo, naive, _ = theoretical_sgp_prob("nfl", legs)
    assert theo > naive
    assert theo > 0.34 and theo < 0.45  # structural band


def test_known_rho_known_fair_price():
    """Sanity: bivariate with rho~0.40 and p=(0.55,0.60) sits near 0.356 per sgp.py docstring."""
    legs = [
        SGPLeg(leg_type="x_over", description="x", american_odds=-122, fair_prob=0.55),
        SGPLeg(leg_type="y_over", description="y", american_odds=-150, fair_prob=0.60),
    ]
    # Inject an explicit 0.40 correlation via YAML override
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as td:
        cfg = Path(td) / "cfg"
        cfg.mkdir()
        (cfg / "sgp_correlations.yaml").write_text(
            "nfl:\n  x_over|y_over: 0.40\n", encoding="utf-8",
        )
        sc.reset_cache()
        sc.load(config_dir=cfg, force_reload=True)
        theo, naive, _ = theoretical_sgp_prob("nfl", legs)

    # tools.sgp docstring: p=(0.55,0.60), rho=0.40 -> joint~0.356
    assert theo == pytest.approx(0.356, abs=0.01)
    assert naive == pytest.approx(0.33, abs=0.005)


# ---------------------------------------------------------------------------
# Enumeration & scanner behavior
# ---------------------------------------------------------------------------

def test_enumerate_skips_duplicate_player_legs():
    """Two over-legs on the same player/line/market should collapse."""
    legs = [
        SGPLeg(leg_type="qb_pass_yds_over", description="M over 275.5",
               american_odds=-110, fair_prob=0.5, player="Mahomes",
               market="player_pass_yds", side="over", line=275.5),
        SGPLeg(leg_type="qb_pass_yds_over", description="M over 275.5 (dup)",
               american_odds=-110, fair_prob=0.5, player="Mahomes",
               market="player_pass_yds", side="over", line=275.5),
        SGPLeg(leg_type="team_total_over", description="Chiefs team O",
               american_odds=-110, fair_prob=0.5, team="Chiefs",
               market="team_totals", side="over", line=27.5),
    ]
    candidates = enumerate_candidates(legs, min_legs=2, max_legs=2)
    # Only one unique combo: (M-over, Chiefs team O). The duplicate
    # Mahomes-over leg is filtered.
    pairs = {tuple(sorted((c[0].description, c[1].description))) for c in candidates}
    assert len(pairs) == 2  # (dup+dup is filtered), (orig+team), (dup+team)
    # No combo should have two identical leg fingerprints
    for c in candidates:
        fps = {(l.player, l.market, l.side, l.line) for l in c}
        assert len(fps) == len(c)


def test_synthesize_book_sgp_price_adds_juice():
    """Synthesized book price must be worse for bettor than naive."""
    legs = [
        SGPLeg(leg_type="a", description="a", american_odds=-110, fair_prob=0.5),
        SGPLeg(leg_type="b", description="b", american_odds=-110, fair_prob=0.5),
    ]
    naive_price = _implied_to_american(
        _american_to_implied(-110) * _american_to_implied(-110)
    )
    book_price = synthesize_book_sgp_price(legs, sgp_juice_pct=0.10)
    # Book price should have shorter odds (higher implied prob)
    assert _american_to_implied(book_price) > _american_to_implied(naive_price)


def test_mock_dk_fetcher_used_when_provided():
    """scan_sgp_edges must call a caller-provided fetcher and use its price."""
    legs = [
        SGPLeg(leg_type="pc_a_over", description="A", american_odds=-110, fair_prob=0.55),
        SGPLeg(leg_type="pc_b_over", description="B", american_odds=-110, fair_prob=0.60),
    ]
    # Set strong positive correlation so the theoretical prob exceeds book
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as td:
        cfg = Path(td) / "cfg"
        cfg.mkdir()
        (cfg / "sgp_correlations.yaml").write_text(
            "nfl:\n  pc_a_over|pc_b_over: 0.55\n", encoding="utf-8",
        )
        sc.reset_cache()
        sc.load(config_dir=cfg, force_reload=True)

        seen_calls: list = []

        def mock_dk(sport, event_id, book, legs_):
            seen_calls.append((sport, event_id, book, len(legs_)))
            # Book quotes +225 — much worse than fair
            return 225

        edges = scan_sgp_edges(
            sport="nfl",
            event_id="evt-1",
            legs=legs,
            book="draftkings",
            fetch_book_sgp=mock_dk,
            threshold=0.01,
        )
    assert seen_calls, "fetcher must be invoked"
    assert seen_calls[0][2] == "draftkings"
    # There should be at least one edge with book_price_source=fetched
    assert edges
    assert edges[0].meta["book_price_source"] == "fetched"
    assert edges[0].book_price_american == 225


def test_edge_suppressed_when_book_quotes_fair():
    """If book quotes exactly the theoretical fair, no edge should be emitted."""
    legs = [
        SGPLeg(leg_type="sx_a_over", description="A", american_odds=-110, fair_prob=0.55),
        SGPLeg(leg_type="sx_b_over", description="B", american_odds=-110, fair_prob=0.60),
    ]
    from tempfile import TemporaryDirectory
    with TemporaryDirectory() as td:
        cfg = Path(td) / "cfg"
        cfg.mkdir()
        (cfg / "sgp_correlations.yaml").write_text(
            "nfl:\n  sx_a_over|sx_b_over: 0.35\n", encoding="utf-8",
        )
        sc.reset_cache()
        sc.load(config_dir=cfg, force_reload=True)

        theo, _naive, _ = theoretical_sgp_prob("nfl", legs)
        fair_price = _implied_to_american(theo)

        def mock(sport, event_id, book, legs_):
            return fair_price

        edges = scan_sgp_edges(
            sport="nfl",
            event_id="evt-2",
            legs=legs,
            book="draftkings",
            fetch_book_sgp=mock,
            threshold=0.01,
        )
    assert edges == []


def test_legs_from_game_odds_extracts_standard_markets():
    game = {
        "id": "g1",
        "home_team": "Home",
        "away_team": "Away",
        "bookmakers": [
            {
                "key": "draftkings",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Home", "price": -200},
                            {"name": "Away", "price": +170},
                        ],
                    },
                    {
                        "key": "totals",
                        "outcomes": [
                            {"name": "Over", "price": -110, "point": 47.5},
                            {"name": "Under", "price": -110, "point": 47.5},
                        ],
                    },
                ],
            }
        ],
    }
    legs = legs_from_game_odds(game, book_priority=("draftkings",))
    types = {l.leg_type for l in legs}
    assert "team_ml_win" in types
    assert "game_total_over" in types
    assert "game_total_under" in types


def test_empty_or_single_leg_returns_empty():
    assert scan_sgp_edges("nfl", "e", [], book="draftkings") == []
    assert scan_sgp_edges(
        "nfl", "e",
        [SGPLeg(leg_type="a", description="a", american_odds=-110, fair_prob=0.5)],
        book="draftkings",
    ) == []


# ---------------------------------------------------------------------------
# Historical calibration smoke-test (synthetic data)
# ---------------------------------------------------------------------------

def test_calibration_recovers_seeded_value(tmp_path, monkeypatch):
    """Feed synthetic events with a known Bernoulli correlation; the calibrator
    should emit a rho within 0.15 of the seeded value."""
    import sqlite3
    import random

    # Build a tiny SQLite DB with a backtest_events table and paper_trades stub
    db_path = tmp_path / "cal.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE backtest_events ("
        "sport TEXT, event_id TEXT, local_game_date TEXT, game_date TEXT,"
        " market TEXT, side TEXT, player TEXT, actual_result TEXT)"
    )
    # Seed 400 events; for each event produce two correlated Bernoulli legs
    # with target rho≈0.45. Method: generate latent z ~ N(0,1); A = (z+eps_A>0),
    # B = (rho*z+sqrt(1-rho^2)*eps_B > 0). This gives phi≈rho for p=0.5.
    import math
    rng = random.Random(1337)
    # Generate two Bernoulli variables with phi = rho_target directly.
    # p(A=1)=p(B=1)=0.5; choose joint p(A=B=1) = 0.25 + rho_target/4 which
    # gives Pearson-phi == rho_target exactly.
    rho_target = 0.45
    # For p(A=1)=p(B=1)=0.5, phi = (p11 - 0.25) / 0.25.
    # Pick p11, p00 such that phi == rho_target and marginals stay 0.5.
    p11 = 0.25 + rho_target * 0.25
    p00 = 0.25 + rho_target * 0.25
    p10 = 0.5 - p11
    p01 = 0.5 - p11
    cdf = [p00, p00 + p01, p00 + p01 + p10, 1.0]

    def _sample() -> tuple[int, int]:
        u = rng.random()
        if u < cdf[0]:
            return (0, 0)
        if u < cdf[1]:
            return (0, 1)
        if u < cdf[2]:
            return (1, 0)
        return (1, 1)

    rows = []
    for i in range(800):
        a, b = _sample()
        evt = f"e{i}"
        rows.append(("americanfootball_nfl", evt, "2026-01-01", "2026-01-01",
                     "player_pass_yds", "over", "QB",
                     "win" if a else "loss"))
        rows.append(("americanfootball_nfl", evt, "2026-01-01", "2026-01-01",
                     "player_receiving_yds", "over", "WR",
                     "win" if b else "loss"))
    # end loop
    con.executemany(
        "INSERT INTO backtest_events "
        "(sport, event_id, local_game_date, game_date, market, side, player, actual_result) "
        "VALUES (?,?,?,?,?,?,?,?)",
        rows,
    )
    con.commit()
    con.close()

    # Run the calibrator as a function
    sys_path_add = _REPO_ROOT / "scripts"
    if str(sys_path_add) not in sys.path:
        sys.path.insert(0, str(sys_path_add))
    import importlib
    calibrate_mod = importlib.import_module("calibrate_sgp_correlations")

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows_read = calibrate_mod._fetch_rows(conn)
    conn.close()
    out = calibrate_mod.calibrate(rows_read, min_samples=30)
    assert "nfl" in out
    key = "qb_pass_yds_over|wr_rec_yds_over"
    assert key in out["nfl"] or key in out["nfl"].keys()
    rho_observed = out["nfl"][key]
    # Observed phi should match target within 0.1 per task brief.
    assert abs(rho_observed - rho_target) < 0.1, (
        f"observed={rho_observed}, target={rho_target}"
    )
