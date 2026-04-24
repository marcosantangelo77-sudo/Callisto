from __future__ import annotations

import asyncio
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import aiosqlite
import pytest

from tools import sgp_correlations as sc
from tools.correlation import (
    _adjust_joint_probability,
    correlated_parlay_odds,
    get_correlation,
    set_learned_store,
)
from tools.learned_correlations import (
    LearnedCorrelationStore,
    get_correlation_hits,
    reset_correlation_hits,
    sport_prior_fallback,
)
from tools.sgp import correlated_parlay_prob
from tools.sgp_scanner import SGPLeg, scan_sgp_edges


_SCHEMA_DDL = """
CREATE TABLE learned_correlations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sport TEXT NOT NULL,
    market_a TEXT NOT NULL,
    market_b TEXT NOT NULL,
    n INTEGER NOT NULL DEFAULT 0,
    mean_a REAL DEFAULT 0,
    mean_b REAL DEFAULT 0,
    m2_a REAL DEFAULT 0,
    m2_b REAL DEFAULT 0,
    co_moment REAL DEFAULT 0,
    pearson_r REAL DEFAULT 0,
    ci_low REAL DEFAULT -1,
    ci_high REAL DEFAULT 1,
    last_updated DATETIME DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(sport, market_a, market_b)
)
"""


async def _make_store() -> LearnedCorrelationStore:
    store = LearnedCorrelationStore(db_path=":memory:")
    store._db = await aiosqlite.connect(":memory:")
    await store._db.execute(_SCHEMA_DDL)
    await store._db.commit()
    return store


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro) if False else asyncio.get_event_loop().run_until_complete(coro)


def _sync(coro):
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("closed")
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
    return loop.run_until_complete(coro)


@pytest.fixture(autouse=True)
def _reset_state():
    sc.reset_cache()
    reset_correlation_hits()
    set_learned_store(None)
    yield
    sc.reset_cache()
    reset_correlation_hits()
    set_learned_store(None)


def test_known_input_correlation_fit():
    async def _body():
        store = await _make_store()
        for i in range(1, 51):
            await store.update("nba", "player_points", "team_total",
                               float(i), float(i * 2))
        est = await store.get("nba", "player_points", "team_total")
        assert est is not None
        assert est.n == 50
        assert est.pearson_r == pytest.approx(1.0, abs=1e-6)
        await store._db.close()
    _sync(_body())


def test_anticorrelated_fit():
    async def _body():
        store = await _make_store()
        for i in range(1, 41):
            await store.update("nfl", "qb_interceptions", "team_total",
                               float(i), float(-i))
        est = await store.get("nfl", "qb_interceptions", "team_total")
        assert est is not None
        assert est.pearson_r == pytest.approx(-1.0, abs=1e-6)
        await store._db.close()
    _sync(_body())


def test_symmetry_on_learned_pair():
    async def _body():
        store = await _make_store()
        for i in range(1, 21):
            await store.update("mlb", "batter_hits", "team_total",
                               float(i), float(i + 2))
        report = store.verify_symmetry()
        assert report["symmetric"] is True
        await store._db.close()
    _sync(_body())


def test_correlated_parlay_prob_gt_naive_on_positive_correlation():
    probs = [0.55, 0.60]
    naive = probs[0] * probs[1]
    zero_rho = correlated_parlay_prob(probs, [[1.0, 0.0], [0.0, 1.0]])
    pos_rho = correlated_parlay_prob(probs, [[1.0, 0.7], [0.7, 1.0]])
    assert zero_rho == pytest.approx(naive, abs=0.005)
    assert pos_rho > naive + 0.03


def test_correlated_parlay_prob_lt_naive_on_negative_correlation():
    probs = [0.55, 0.60]
    naive = probs[0] * probs[1]
    neg_rho = correlated_parlay_prob(probs, [[1.0, -0.5], [-0.5, 1.0]])
    assert neg_rho < naive


def test_adjust_joint_probability_clamped_within_frechet_hoeffding():
    j = _adjust_joint_probability(0.2, 0.3, 1.0)
    assert j <= min(0.2, 0.3) + 1e-9
    j2 = _adjust_joint_probability(0.8, 0.9, -1.0)
    assert j2 >= max(0.0, 0.8 + 0.9 - 1.0) - 1e-9


def test_correlated_parlay_odds_boost_on_positive_corr():
    legs = [
        {"american_odds": -110, "market": "qb_passing_yards"},
        {"american_odds": -110, "market": "team_total"},
    ]
    indep = correlated_parlay_odds(
        legs, correlations={("qb_passing_yards", "team_total"): 0.0}, sport="nfl"
    )
    pos = correlated_parlay_odds(
        legs, correlations={("qb_passing_yards", "team_total"): 0.7}, sport="nfl"
    )
    import tools.correlation as corr
    indep_p = corr._american_to_implied(indep)
    pos_p = corr._american_to_implied(pos)
    assert pos_p > indep_p


def test_sgp_scanner_fallback_uses_sport_prior_not_zero():
    legs = [
        SGPLeg(
            leg_type="this_is_totally_unknown_over",
            description="Unknown leg A",
            american_odds=-110,
            fair_prob=0.52,
            player="X",
        ),
        SGPLeg(
            leg_type="also_totally_unknown_over",
            description="Unknown leg B",
            american_odds=-110,
            fair_prob=0.52,
            player="Y",
        ),
    ]
    scan_sgp_edges("nba", "evt-fake", legs, threshold=-1.0)
    hits = get_correlation_hits()
    assert hits.get("fallback", 0) >= 1


def test_sport_prior_fallback_returns_configured_value():
    assert sport_prior_fallback("nba") > 0
    assert sport_prior_fallback("nfl") > 0
    assert sport_prior_fallback("unknown_sport") == 0.0


def test_correlation_hits_learned_source_via_store():
    async def _body():
        store = await _make_store()
        import random
        rng = random.Random(17)
        for i in range(1, 121):
            noise = rng.gauss(0, 8)
            await store.update(
                "nfl", "qb_passing_yards", "team_total",
                250.0 + i + noise, 22.0 + i * 0.05 + rng.gauss(0, 1.0),
            )
        est = await store.get("nfl", "qb_passing_yards", "team_total")
        assert est is not None and est.n >= 100
        assert (est.ci_high - est.ci_low) <= 0.3
        set_learned_store(store)
        reset_correlation_hits()
        get_correlation("qb_passing_yards", "team_total", "nfl")
        hits = get_correlation_hits()
        assert hits.get("learned", 0) == 1
        await store._db.close()
    _sync(_body())


def test_metadata_contains_last_trained_and_per_sport():
    async def _body():
        store = await _make_store()
        for i in range(1, 31):
            await store.update("nba", "player_points", "team_total",
                               float(20 + i), float(105 + i * 0.5))
        md = store.metadata()
        assert "last_trained_at" in md
        assert "per_sport" in md
        assert "nba" in md["per_sport"]
        assert md["per_sport"]["nba"]["trained_pairs"] >= 1
        await store._db.close()
    _sync(_body())


def test_sgp_correlations_yaml_override_still_wins_with_no_learned(tmp_path):
    cfg = tmp_path / "config"
    cfg.mkdir()
    (cfg / "sgp_correlations.yaml").write_text(
        "nfl:\n  qb_pass_yds_over|wr_rec_yds_over: 0.55\n",
        encoding="utf-8",
    )
    sc.reset_cache()
    tbl = sc.load(config_dir=cfg, force_reload=True)
    assert tbl.get("nfl", "qb_pass_yds_over", "wr_rec_yds_over") == pytest.approx(0.55)
    assert tbl.source("nfl", "qb_pass_yds_over", "wr_rec_yds_over") == "yaml_override"


def test_min_observations_gate_blocks_premature_learned():
    async def _body():
        store = await _make_store()
        for _ in range(3):
            await store.update("nfl", "qb_passing_yards", "team_total",
                               250.0, 22.0)
        assert store.get_blended("nfl", "qb_passing_yards", "team_total",
                                 prior=0.65) == 0.65
        await store._db.close()
    _sync(_body())
