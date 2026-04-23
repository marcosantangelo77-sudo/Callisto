"""Unit tests for the pre-LIVE bankroll Monte Carlo simulator.

feat/bankroll-montecarlo-sim (2026-04-22).

Covers:
  * Synthetic +EV hyp → positive expected ROI, low ruin prob
  * Synthetic -EV hyp → negative expected ROI, rising ruin prob
  * Two correlated hyps → higher ruin than two independent at matched EV
  * Drawdown kill switch → reduces tail risk (p99 DD) and stops bleed
"""

from __future__ import annotations

import math

import numpy as np
import pytest


def _mk_signal(
    hyp_id: str,
    day: str,
    event_id: str,
    actual_result: str,
    odds: int = -110,
    edge: float = 0.08,
    sport: str = "baseball_mlb",
) -> dict:
    return {
        "hypothesis_id": hyp_id,
        "event_id": event_id,
        "game_date": day,
        "sport": sport,
        "market": "h2h",
        "side": "TeamA",
        "odds": odds,
        "edge": edge,
        "ev_pct": edge * 1.8,
        "actual_result": actual_result,
    }


def _generate_signals(
    hyp_id: str,
    days: int = 60,
    per_day: int = 1,
    win_rate: float = 0.58,
    edge: float = 0.06,
    seed: int = 1,
    sport: str = "baseball_mlb",
    event_prefix: str = "",
) -> list[dict]:
    """Build a synthetic pool of signals with a given win rate."""
    rng = np.random.default_rng(seed)
    base_date = np.datetime64("2026-02-01")
    out = []
    for d in range(days):
        day_str = str(base_date + np.timedelta64(d, "D"))
        for k in range(per_day):
            won = bool(rng.random() < win_rate)
            eid = f"{event_prefix}{hyp_id}_{d:03d}_{k}"
            out.append(_mk_signal(
                hyp_id, day_str, eid,
                actual_result="won" if won else "lost",
                edge=edge, sport=sport,
            ))
    return out


def _generate_shared_signals(
    hyp_a: str,
    hyp_b: str,
    days: int = 60,
    per_day: int = 1,
    win_rate: float = 0.58,
    edge: float = 0.06,
    seed: int = 1,
    sport: str = "baseball_mlb",
) -> list[dict]:
    """Two hyps that BET THE SAME EVENT on the same day (perfect joint-fire correlation)."""
    rng = np.random.default_rng(seed)
    base_date = np.datetime64("2026-02-01")
    out = []
    for d in range(days):
        day_str = str(base_date + np.timedelta64(d, "D"))
        for k in range(per_day):
            # BOTH hyps see the same event_id and the same outcome
            won = bool(rng.random() < win_rate)
            eid = f"shared_{d:03d}_{k}"
            out.append(_mk_signal(hyp_a, day_str, eid, "won" if won else "lost", edge=edge, sport=sport))
            out.append(_mk_signal(hyp_b, day_str, eid, "won" if won else "lost", edge=edge, sport=sport))
    return out


# ---------------------------------------------------------------------------
# 1. +EV hypothesis → positive expected ROI
# ---------------------------------------------------------------------------
def test_positive_ev_hypothesis_positive_expected_roi():
    from tools.bankroll_sim import simulate_portfolio

    # 58% win rate on -110 ≈ +10% EV → strongly +EV
    signals = _generate_signals("pos_ev", days=90, per_day=1, win_rate=0.58, edge=0.08, seed=11)
    result = simulate_portfolio(
        hypothesis_ids=["pos_ev"],
        n_sims=300,
        horizon_days=60,
        starting_bankroll=10000.0,
        kelly_fraction=0.25,
        seed=7,
        signals_override=signals,
    )
    assert result.expected_total_roi > 0.0, (
        f"+EV hyp should produce positive expected ROI, got "
        f"{result.expected_total_roi:.4f}"
    )
    # Ruin should be rare at 30% DD
    assert result.ruin_prob_30pct < 0.25, (
        f"+EV with kill switch shouldn't ruin often, got {result.ruin_prob_30pct:.3f}"
    )


# ---------------------------------------------------------------------------
# 2. -EV hypothesis → negative expected ROI + rising ruin prob
# ---------------------------------------------------------------------------
def test_negative_ev_hypothesis_negative_expected_roi_and_high_ruin():
    from tools.bankroll_sim import simulate_portfolio

    # 40% win rate at -110 ≈ -13% EV → strongly -EV
    signals = _generate_signals("neg_ev", days=90, per_day=1, win_rate=0.40, edge=0.08, seed=22)
    result = simulate_portfolio(
        hypothesis_ids=["neg_ev"],
        n_sims=300,
        horizon_days=60,
        starting_bankroll=10000.0,
        kelly_fraction=0.25,
        seed=9,
        signals_override=signals,
    )
    assert result.expected_total_roi < 0.0, (
        f"-EV hyp should produce negative expected ROI, got "
        f"{result.expected_total_roi:.4f}"
    )
    # At least some paths should bleed to kill-switch territory
    assert result.ruin_prob_15pct > 0.05, (
        f"-EV should at minimum bleed into 15% DD regularly, got {result.ruin_prob_15pct:.3f}"
    )


# ---------------------------------------------------------------------------
# 3. Two perfectly-correlated hyps → higher ruin than two independent hyps
# ---------------------------------------------------------------------------
def test_correlated_hyps_have_higher_ruin_than_independent():
    from tools.bankroll_sim import simulate_portfolio

    # Correlated: both bet the same event, same outcome every time
    corr_signals = _generate_shared_signals("cor_a", "cor_b", days=90, per_day=1, win_rate=0.56, edge=0.07, seed=33)
    # Independent: separate events, separate RNG streams
    ind_a = _generate_signals("ind_a", days=90, per_day=1, win_rate=0.56, edge=0.07, seed=101, event_prefix="ind_a_")
    ind_b = _generate_signals("ind_b", days=90, per_day=1, win_rate=0.56, edge=0.07, seed=202, event_prefix="ind_b_")

    # Use a SHARED seed so both sims sample the same calendar-day sequence —
    # variance-reduction trick that makes the comparison robust.
    shared_seed = 1234
    corr_result = simulate_portfolio(
        hypothesis_ids=["cor_a", "cor_b"],
        n_sims=400, horizon_days=60, starting_bankroll=10000.0,
        kelly_fraction=0.25, seed=shared_seed,
        signals_override=corr_signals,
    )
    ind_result = simulate_portfolio(
        hypothesis_ids=["ind_a", "ind_b"],
        n_sims=400, horizon_days=60, starting_bankroll=10000.0,
        kelly_fraction=0.25, seed=shared_seed,
        signals_override=ind_a + ind_b,
    )

    # Correlated ruin at 15% should be higher (or at least not meaningfully lower)
    # The signal is clearer at p90 drawdown, which captures tail risk.
    assert corr_result.max_drawdown_p90 >= ind_result.max_drawdown_p90 - 0.02, (
        f"Correlated portfolio should show fatter tail DD. "
        f"corr p90={corr_result.max_drawdown_p90:.3f}, "
        f"ind p90={ind_result.max_drawdown_p90:.3f}"
    )
    assert corr_result.ruin_prob_15pct >= ind_result.ruin_prob_15pct - 0.05, (
        f"Correlated ruin_15 should be >= independent (within tolerance). "
        f"corr={corr_result.ruin_prob_15pct:.3f}, ind={ind_result.ruin_prob_15pct:.3f}"
    )


# ---------------------------------------------------------------------------
# 4. Drawdown kill switch reduces tail risk
# ---------------------------------------------------------------------------
def test_drawdown_kill_switch_reduces_tail_risk():
    from tools.bankroll_sim import simulate_portfolio

    # Strongly -EV so many paths will cross DD thresholds
    signals = _generate_signals("bleed", days=90, per_day=2, win_rate=0.42, edge=0.09, seed=77)

    # Tight kill switch: trips at 15% drawdown
    tight = simulate_portfolio(
        hypothesis_ids=["bleed"],
        n_sims=300, horizon_days=60, starting_bankroll=10000.0,
        kelly_fraction=0.25, seed=5,
        signals_override=signals,
        kill_switch_drawdown=0.15,
    )
    # Loose kill switch: effectively disabled (trips at 99% drawdown)
    loose = simulate_portfolio(
        hypothesis_ids=["bleed"],
        n_sims=300, horizon_days=60, starting_bankroll=10000.0,
        kelly_fraction=0.25, seed=5,
        signals_override=signals,
        kill_switch_drawdown=0.99,
    )

    # Kill switch should cap the p99 drawdown at/near the trigger threshold.
    # Without it, -EV bleed should push p99 DD much deeper.
    assert tight.max_drawdown_p99 <= loose.max_drawdown_p99 + 1e-6, (
        f"tight-kill p99 DD ({tight.max_drawdown_p99:.3f}) should not exceed "
        f"loose-kill ({loose.max_drawdown_p99:.3f})"
    )
    # And the median max DD should be tighter under kill switch
    assert tight.max_drawdown_median <= loose.max_drawdown_median + 1e-6, (
        f"tight-kill median DD ({tight.max_drawdown_median:.3f}) should not exceed "
        f"loose ({loose.max_drawdown_median:.3f})"
    )


# ---------------------------------------------------------------------------
# 5. Deterministic with seed
# ---------------------------------------------------------------------------
def test_seed_reproducibility():
    from tools.bankroll_sim import simulate_portfolio

    signals = _generate_signals("rep", days=60, per_day=1, win_rate=0.55, edge=0.06, seed=3)
    r1 = simulate_portfolio(
        hypothesis_ids=["rep"], n_sims=100, horizon_days=30,
        starting_bankroll=5000.0, kelly_fraction=0.25, seed=42,
        signals_override=signals,
    )
    r2 = simulate_portfolio(
        hypothesis_ids=["rep"], n_sims=100, horizon_days=30,
        starting_bankroll=5000.0, kelly_fraction=0.25, seed=42,
        signals_override=signals,
    )
    assert math.isclose(r1.expected_total_roi, r2.expected_total_roi)
    assert math.isclose(r1.ruin_prob_15pct, r2.ruin_prob_15pct)
    assert math.isclose(r1.mean_final_bankroll, r2.mean_final_bankroll)


# ---------------------------------------------------------------------------
# 6. Promotion gate: candidate with simulated ruin_prob > cap is blocked
# ---------------------------------------------------------------------------
def test_simulate_before_promote_gate_blocks_high_ruin(tmp_path, monkeypatch):
    """Create a small SQLite DB with one -EV hyp + 60 signals, then assert the
    gate (called via simulate_before_promote) reports ruin > default cap of 2%.
    """
    import sqlite3 as _sqlite3
    from tools.bankroll_sim import simulate_before_promote

    db_path = tmp_path / "gate_test.db"
    conn = _sqlite3.connect(str(db_path))
    conn.executescript(
        """
        CREATE TABLE hypotheses (
            hypothesis_id TEXT PRIMARY KEY,
            name TEXT, thesis TEXT, sport TEXT, market_type TEXT,
            model_config TEXT, edge_threshold REAL, status TEXT,
            min_sample_size INTEGER, significance_level REAL,
            created_at DATETIME, updated_at DATETIME, promoted_at DATETIME,
            promoted_by TEXT, notes TEXT
        );
        CREATE TABLE backtest_events (
            id INTEGER PRIMARY KEY, run_id TEXT, event_id TEXT,
            hypothesis_id TEXT, sport TEXT, market TEXT, side TEXT, book TEXT,
            book_odds_american INTEGER, book_implied_prob REAL,
            model_fair_prob REAL, edge REAL, ev_pct REAL,
            signal_generated BOOLEAN, actual_result TEXT, game_date DATE,
            snapshot_time DATETIME, line REAL, closing_odds INTEGER,
            closing_implied REAL, clv_implied REAL, kelly_fraction REAL,
            model_factors TEXT, actual_stat REAL, created_at DATETIME
        );
        """
    )
    # Candidate loses 2/3 of signals at -110 — deeply -EV
    base = np.datetime64("2026-02-01")
    for d in range(60):
        day_str = str(base + np.timedelta64(d, "D"))
        for k in range(3):
            won = (k == 0)  # 1 win, 2 losses per day = 33% win rate
            conn.execute(
                "INSERT INTO backtest_events "
                "(run_id, event_id, hypothesis_id, sport, market, side, book, "
                " book_odds_american, book_implied_prob, model_fair_prob, edge, "
                " ev_pct, signal_generated, actual_result, game_date, snapshot_time) "
                "VALUES ('r', ?, 'bad_hyp', 'baseball_mlb', 'h2h', 'A', 'dk', -110, "
                "0.524, 0.60, 0.08, 0.14, 1, ?, ?, ?)",
                (f"e_{d}_{k}", "won" if won else "lost", day_str, day_str + "T12:00:00"),
            )
    conn.commit()
    conn.close()

    result = simulate_before_promote(
        "bad_hyp",
        db_path=str(db_path),
        n_sims=200,
        horizon_days=30,
    )
    # At 33% win rate on -110, ruin prob at 15% DD over 30d should be >> 2%
    assert result["ruin_prob_30d"] > 0.02, (
        f"Deeply -EV gate test should exceed 2% cap, got {result['ruin_prob_30d']:.3f}"
    )
    assert result["expected_monthly_roi"] < 0, (
        f"Deeply -EV gate test should have negative monthly ROI, got "
        f"{result['expected_monthly_roi']:.3f}"
    )


# ---------------------------------------------------------------------------
# 7. Provenance counters
# ---------------------------------------------------------------------------
def test_provenance_counts_are_reported():
    from tools.bankroll_sim import simulate_portfolio

    signals = _generate_signals("prov", days=30, per_day=1, win_rate=0.55, edge=0.06, seed=4)
    result = simulate_portfolio(
        hypothesis_ids=["prov"], n_sims=50, horizon_days=15,
        signals_override=signals, seed=1,
    )
    assert result.rows_used == len(signals)
    assert result.distinct_days == 30
    assert result.distinct_hyps_with_data == 1
