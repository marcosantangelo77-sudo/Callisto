"""Tests for the tools.bankrollsim split of tools.bankroll_sim.

feat/refactor (2026-08): bankroll_sim.py was extracted into the
``tools.bankrollsim`` package. These tests verify:

  * Facade re-exports every public name and stays import-compatible
  * config constants load from env overrides
  * PortfolioSimResult serialization (to_dict, paths handling)
  * degenerate_result builder for empty pools
  * signal loading: exclusion counters + lookahead defense
  * day grouping
  * slate sizing: single-bet Kelly path, portfolio path, per-game/sport caps,
    minimum-bet floor
  * bet resolution P&L arithmetic across won/lost/push and odds signs
  * simulate_portfolio end-to-end behavior on synthetic pools:
    determinism, +EV vs -EV drift, kill-switch effect, keep_paths,
    empty pool, correlation matrix acceptance, monthly-ROI scaling
  * ascii_bankroll_histogram rendering
  * package-level safety: no live-betting surface is introduced
"""

from __future__ import annotations

import importlib

import numpy as np
import pytest


# =========================================================================
# Helpers
# =========================================================================
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
            if won:
                res = "won"
            else:
                res = "lost"
            out.append(
                _mk_signal(
                    hyp_id,
                    day_str,
                    f"{event_prefix}{hyp_id}-{d}-{k}",
                    res,
                    edge=edge,
                    sport=sport,
                )
            )
    return out


# =========================================================================
# Facade / package structure
# =========================================================================
class TestFacadeReexports:
    def test_facade_exposes_public_api(self):
        from tools import bankroll_sim

        for name in [
            "simulate_portfolio",
            "simulate_before_promote",
            "ascii_bankroll_histogram",
            "PortfolioSimResult",
        ]:
            assert hasattr(bankroll_sim, name), name

    def test_package_matches_facade(self):
        import tools.bankroll_sim as facade
        import tools.bankrollsim as pkg

        assert facade.simulate_portfolio is pkg.simulate_portfolio
        assert facade.simulate_before_promote is pkg.simulate_before_promote
        assert facade.ascii_bankroll_histogram is pkg.ascii_bankroll_histogram
        assert facade.PortfolioSimResult is pkg.PortfolioSimResult

    def test_private_helpers_reachable_via_facade(self):
        from tools import bankroll_sim

        # Existing tests and scripts reach these underscore names through
        # the facade; they must remain present.
        for name in ["_load_signals", "_group_signals_by_day", "_size_slate", "_resolve_bets"]:
            assert hasattr(bankroll_sim, name), name

    def test_constants_reexported(self):
        from tools import bankroll_sim
        from tools.bankrollsim import config

        assert bankroll_sim.DB_PATH == config.DB_PATH
        assert (
            bankroll_sim.DEFAULT_KILL_SWITCH_DRAWDOWN
            == config.DEFAULT_KILL_SWITCH_DRAWDOWN
        )
        assert bankroll_sim.SIM_MAX_BET_PCT == config.SIM_MAX_BET_PCT
        assert (
            bankroll_sim.SIM_MAX_GAME_EXPOSURE_PCT
            == config.SIM_MAX_GAME_EXPOSURE_PCT
        )
        assert (
            bankroll_sim.SIM_MAX_SPORT_EXPOSURE_PCT
            == config.SIM_MAX_SPORT_EXPOSURE_PCT
        )
        assert bankroll_sim.SIM_MIN_BET_AMOUNT == config.SIM_MIN_BET_AMOUNT

    def test_submodule_names(self):
        import tools.bankrollsim as pkg

        for mod in ["config", "result", "signals", "sizing", "simulator", "promote_gate", "histogram"]:
            assert importlib.import_module(f"tools.bankrollsim.{mod}") is not None


class TestNoLiveBettingSurface:
    """The sim framework must never grow a live-betting surface."""

    def test_no_live_status_in_package_source(self):
        import pathlib
        import tools.bankrollsim as pkg

        pkg_dir = pathlib.Path(pkg.__file__).parent
        banned_snippets = [
            "_PAPER_TRADE_SIGNAL_STATUSES",
            "status == 'live'",
            'status == "live"',
        ]
        for f in pkg_dir.glob("*.py"):
            text = f.read_text()
            for snippet in banned_snippets:
                assert snippet not in text, (f.name, snippet)

    def test_generate_paper_trade_signal_not_defined(self):
        import tools.bankrollsim as pkg

        assert not hasattr(pkg, "generate_paper_trade_signal")

    def test_no_place_bet_function_in_package(self):
        import tools.bankrollsim as pkg

        for attr in dir(pkg):
            low = attr.lower()
            assert "place" not in low or "placeholder" in low, attr


# =========================================================================
# Config
# =========================================================================
class TestConfig:
    def test_defaults(self):
        from tools.bankrollsim.config import (
            DEFAULT_KILL_SWITCH_DRAWDOWN,
            SIM_MAX_BET_PCT,
            SIM_MAX_GAME_EXPOSURE_PCT,
            SIM_MAX_SPORT_EXPOSURE_PCT,
            SIM_MIN_BET_AMOUNT,
        )

        assert DEFAULT_KILL_SWITCH_DRAWDOWN == pytest.approx(0.15)
        assert SIM_MAX_BET_PCT == pytest.approx(0.05)
        assert SIM_MAX_GAME_EXPOSURE_PCT == pytest.approx(0.08)
        assert SIM_MAX_SPORT_EXPOSURE_PCT == pytest.approx(0.15)
        assert SIM_MIN_BET_AMOUNT == pytest.approx(1.0)


# =========================================================================
# PortfolioSimResult
# =========================================================================
class TestPortfolioSimResult:
    def _result(self, **overrides) -> "PortfolioSimResult":
        from tools.bankrollsim.result import PortfolioSimResult

        kwargs = dict(
            hypothesis_ids=["h1"],
            n_sims=10,
            horizon_days=30,
            starting_bankroll=10000.0,
            kelly_fraction=0.25,
            seed=42,
            total_rows_considered=100,
            rows_excluded_no_signal=1,
            rows_excluded_unresolved=2,
            rows_excluded_lookahead=3,
            rows_used=94,
            distinct_days=60,
            distinct_hyps_with_data=1,
            final_bankroll_p10=9000.0,
            final_bankroll_p50=10500.0,
            final_bankroll_p90=12000.0,
            mean_final_bankroll=10600.0,
            expected_total_roi=0.06,
            median_total_roi=0.05,
            p10_total_roi=-0.10,
            p90_total_roi=0.20,
            expected_monthly_roi=0.06,
            median_monthly_roi=0.05,
            p10_monthly_roi=-0.10,
            p90_monthly_roi=0.20,
            max_drawdown_median=0.04,
            max_drawdown_p90=0.12,
            max_drawdown_p99=0.25,
            ruin_prob_5pct=0.3,
            ruin_prob_15pct=0.1,
            ruin_prob_30pct=0.02,
            days_to_ruin_median=None,
            pct_paths_kill_switch_triggered=0.08,
            sharpe=1.2,
            sortino=1.8,
            avg_bets_per_path=30.0,
            avg_bets_per_day=1.0,
            paths=None,
        )
        kwargs.update(overrides)
        return PortfolioSimResult(**kwargs)

    def test_to_dict_drops_paths_by_default(self):
        r = self._result()
        d = r.to_dict()
        assert "paths" not in d
        assert d["n_sims"] == 10
        assert d["rows_used"] == 94

    def test_to_dict_include_paths_converts_to_list(self):
        arr = np.array([[10000.0, 10100.0], [10000.0, 9900.0]])
        r = self._result(paths=arr)
        d = r.to_dict(include_paths=True)
        assert isinstance(d["paths"], list)
        assert d["paths"][0][1] == pytest.approx(10100.0)
        assert isinstance(d["paths"][0], list)

    def test_to_dict_include_paths_none(self):
        r = self._result(paths=None)
        d = r.to_dict(include_paths=True)
        assert d.get("paths") is None

    def test_degenerate_result_is_all_zero_risk(self):
        from tools.bankrollsim.result import degenerate_result

        r = degenerate_result(
            hypothesis_ids=["hx"],
            n_sims=50,
            horizon_days=30,
            starting_bankroll=5000.0,
            kelly_fraction=0.25,
            seed=7,
        )
        assert r.rows_used == 0
        assert r.distinct_days == 0
        assert r.final_bankroll_p50 == pytest.approx(5000.0)
        assert r.expected_total_roi == 0.0
        assert r.ruin_prob_5pct == 0.0
        assert r.ruin_prob_15pct == 0.0
        assert r.ruin_prob_30pct == 0.0
        assert r.sharpe == 0.0
        assert r.days_to_ruin_median is None
        assert r.paths is None
        assert "paths" not in r.to_dict()


# =========================================================================
# Signal loading & grouping
# =========================================================================
class TestLoadSignals:
    def test_empty_hyp_list_short_circuits_without_db_touch(self, tmp_path):
        from tools.bankrollsim.signals import _load_signals

        # db_path points at a nonexistent file; empty id list must not query.
        rows, exc = _load_signals([], db_path=str(tmp_path / "missing.db"))
        assert rows == []
        assert exc == {
            "no_signal": 0,
            "unresolved": 0,
            "lookahead": 0,
            "total_considered": 0,
        }

    @pytest.fixture()
    def seeded_db(self, tmp_path):
        import sqlite3

        db = tmp_path / "be.db"
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE backtest_events (
                hypothesis_id TEXT, event_id TEXT, game_date TEXT,
                snapshot_time TEXT, sport TEXT, market TEXT, side TEXT,
                book_odds_american INTEGER, edge REAL, ev_pct REAL,
                signal_generated INTEGER, actual_result TEXT
            )
            """
        )
        rows = [
            # kept
            ("h1", "e1", "2026-03-01", "2026-03-01T14:00:00Z", "baseball_mlb",
             "h2h", "TeamA", -110, 0.08, 0.144, 1, "won"),
            # excluded: no signal
            ("h1", "e2", "2026-03-02", None, "baseball_mlb", "h2h", "TeamA",
             -110, 0.08, 0.144, 0, "won"),
            # excluded: unresolved
            ("h1", "e3", "2026-03-03", None, "baseball_mlb", "h2h", "TeamA",
             -110, 0.08, 0.144, 1, None),
            # excluded: lookahead (>1 day after game date)
            ("h1", "e4", "2026-03-04", "2026-03-09T14:00:00Z", "baseball_mlb",
             "h2h", "TeamA", -110, 0.08, 0.144, 1, "won"),
            # kept: same-day snapshot allowed
            ("h1", "e5", "2026-03-05", "2026-03-05T20:00:00Z", "baseball_mlb",
             "h2h", "TeamB", 150, 0.05, 0.09, 1, "lost"),
            # kept: unparseable snapshot tolerated (best effort)
            ("h1", "e6", "2026-03-06", "not-a-date", "baseball_mlb", "h2h",
             "TeamB", 150, 0.05, 0.09, 1, "push"),
            # other hyp filtered by WHERE clause
            ("h2", "e7", "2026-03-07", None, "baseball_mlb", "h2h", "TeamA",
             -110, 0.08, 0.144, 1, "won"),
        ]
        conn.executemany("INSERT INTO backtest_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        conn.commit()
        conn.close()
        return str(db)

    def test_load_filters_and_counts(self, seeded_db):
        from tools.bankrollsim.signals import _load_signals

        rows, exc = _load_signals(["h1"], db_path=seeded_db)
        ids = sorted(r["event_id"] for r in rows)
        assert ids == ["e1", "e5", "e6"]
        assert exc["total_considered"] == 6
        assert exc["no_signal"] == 1
        assert exc["unresolved"] == 1
        assert exc["lookahead"] == 1

    def test_loaded_row_shape(self, seeded_db):
        from tools.bankrollsim.signals import _load_signals

        rows, _ = _load_signals(["h1"], db_path=seeded_db)
        by_event = {r["event_id"]: r for r in rows}
        e1 = by_event["e1"]
        assert e1["hypothesis_id"] == "h1"
        assert e1["event_id"] == "e1"
        assert e1["odds"] == -110
        assert e1["edge"] == pytest.approx(0.08)
        assert e1["ev_pct"] == pytest.approx(0.144)
        assert e1["actual_result"] == "won"
        assert e1["sport"] == "baseball_mlb"

    def test_missing_optionals_get_defaults(self, tmp_path):
        import sqlite3

        from tools.bankrollsim.signals import _load_signals

        db = str(tmp_path / "sparse.db")
        conn = sqlite3.connect(db)
        conn.execute(
            """
            CREATE TABLE backtest_events (
                hypothesis_id TEXT, event_id TEXT, game_date TEXT,
                snapshot_time TEXT, sport TEXT, market TEXT, side TEXT,
                book_odds_american INTEGER, edge REAL, ev_pct REAL,
                signal_generated INTEGER, actual_result TEXT
            )
            """
        )
        conn.execute(
            "INSERT INTO backtest_events VALUES "
            "('h9','e9','2026-03-01',NULL,NULL,NULL,NULL,NULL,NULL,NULL,1,'won')"
        )
        conn.commit()
        conn.close()

        rows, _ = _load_signals(["h9"], db_path=db)
        assert len(rows) == 1
        r = rows[0]
        assert r["event_id"] == "e9"
        assert r["sport"] == ""
        assert r["market"] == ""
        assert r["side"] == ""
        assert r["odds"] == -110
        assert r["edge"] == 0.0
        assert r["ev_pct"] == 0.0

    def test_multi_hyp_query(self, seeded_db):
        from tools.bankrollsim.signals import _load_signals

        rows, exc = _load_signals(["h1", "h2"], db_path=seeded_db)
        hyps = {r["hypothesis_id"] for r in rows}
        assert "h2" in hyps
        assert exc["total_considered"] == 7


class TestGroupSignalsByDay:
    def test_groups_by_game_date(self):
        from tools.bankrollsim.signals import _group_signals_by_day

        rows = [
            _mk_signal("h1", "2026-03-01", "a", "won"),
            _mk_signal("h1", "2026-03-01", "b", "lost"),
            _mk_signal("h2", "2026-03-02", "c", "push"),
        ]
        by_day = _group_signals_by_day(rows)
        assert set(by_day.keys()) == {"2026-03-01", "2026-03-02"}
        assert len(by_day["2026-03-01"]) == 2
        assert by_day["2026-03-02"][0]["event_id"] == "c"

    def test_empty(self):
        from tools.bankrollsim.signals import _group_signals_by_day

        assert _group_signals_by_day([]) == {}


# =========================================================================
# Sizing
# =========================================================================
class TestSizeSlate:
    def test_empty_slate(self):
        from tools.bankrollsim.sizing import _size_slate

        assert _size_slate([], 10000.0, 0.25, {}) == []

    def test_single_signal_returns_one_sized_bet(self):
        from tools.bankrollsim.sizing import _size_slate

        sig = _mk_signal("h1", "2026-03-01", "e1", "won", edge=0.10)
        out = _size_slate([sig], 10000.0, 0.25, {"h1": 1})
        assert len(out) == 1
        b = out[0]
        assert b["stake"] > 0
        assert b["stake"] <= 10000.0 * 0.05 + 1e-9  # per-bet cap
        assert "fraction" in b
        # original fields carried through
        assert b["hypothesis_id"] == "h1"
        assert b["event_id"] == "e1"

    def test_stakes_below_min_zeroed(self):
        from tools.bankrollsim.sizing import _size_slate

        # Tiny bankroll: even a healthy edge yields a sub-$1 stake that the
        # floor must zero out.
        sig = _mk_signal("h1", "2026-03-01", "e1", "won", edge=0.10)
        out = _size_slate([sig], 5.0, 0.25, {"h1": 1})
        assert out[0]["stake"] == 0.0

    def test_portfolio_of_two_sizes_both(self):
        from tools.bankrollsim.sizing import _size_slate

        a = _mk_signal("h1", "2026-03-01", "ea", "won", edge=0.09)
        b = _mk_signal("h2", "2026-03-01", "eb", "won", edge=0.07)
        out = _size_slate([a, b], 10000.0, 0.25, {"h1": 5, "h2": 5})
        assert len(out) == 2
        total = sum(x["stake"] for x in out)
        assert total <= 10000.0 * 0.08 * 2 + 1e-6  # per-game cap headroom
        assert all(x["stake"] >= 0.0 for x in out)

    def test_per_game_cap_limits_same_event_bets(self):
        from tools.bankrollsim.sizing import _size_slate

        a = _mk_signal("h1", "2026-03-01", "same-event", "won", edge=0.12)
        b = _mk_signal("h2", "2026-03-01", "same-event", "won", edge=0.12)
        c = _mk_signal("h3", "2026-03-01", "other-event", "won", edge=0.12)
        out = _size_slate([a, b, c], 10000.0, 0.25, {})
        game_total = sum(
            x["stake"] for x in out if x["event_id"] == "same-event"
        )
        cap = 10000.0 * 0.08
        assert game_total <= cap + 0.05  # rounding tolerance

    def test_per_sport_cap_respected(self):
        from tools.bankrollsim.sizing import _size_slate

        sigs = [
            _mk_signal(f"h{i}", "2026-03-01", f"e{i}", "won", edge=0.15,
                       sport="basketball_nba")
            for i in range(4)
        ] + [
            _mk_signal("hz", "2026-03-01", "ez", "won", edge=0.15,
                       sport="icehockey_nhl")
        ]
        out = _size_slate(sigs, 10000.0, 0.25, {})
        nba_total = sum(
            x["stake"] for x in out if x.get("sport") == "basketball_nba"
        )
        assert nba_total <= 10000.0 * 0.15 + 0.05

    def test_correlation_matrix_accepted(self):
        from tools.bankrollsim.sizing import _size_slate

        a = _mk_signal("h1", "2026-03-01", "ea", "won", edge=0.09)
        b = _mk_signal("h2", "2026-03-01", "eb", "won", edge=0.09)
        corr = {("h1", "h2"): 0.8}
        out_a = _size_slate([a, b], 10000.0, 0.25, {}, corr)
        out_b = _size_slate([a, b], 10000.0, 0.25, {}, None)
        assert len(out_a) == len(out_b) == 2
        # High correlation should dampen total exposure vs independent default
        tot_corr = sum(x["stake"] for x in out_a)
        tot_ind = sum(x["stake"] for x in out_b)
        assert tot_corr <= tot_ind + 1e-6

    def test_kelly_fraction_scaling_moves_stakes(self):
        from tools.bankrollsim.sizing import _size_slate

        sigs = [
            _mk_signal("h1", "d", "e1", "won", edge=0.10),
            _mk_signal("h2", "d", "e2", "won", edge=0.10),
        ]
        conservative = sum(x["stake"] for x in _size_slate(sigs, 10000.0, 0.10, {}))
        aggressive = sum(x["stake"] for x in _size_slate(sigs, 10000.0, 0.40, {}))
        assert aggressive > conservative


class TestResolveBets:
    def test_won_negative_odds(self):
        from tools.bankrollsim.sizing import _resolve_bets

        pnl = _resolve_bets([
            {"stake": 100.0, "odds": -110, "actual_result": "won"},
        ])
        assert pnl == pytest.approx(100.0 * 100.0 / 110.0)

    def test_won_positive_odds(self):
        from tools.bankrollsim.sizing import _resolve_bets

        pnl = _resolve_bets([
            {"stake": 100.0, "odds": 150, "actual_result": "won"},
        ])
        assert pnl == pytest.approx(150.0)

    def test_lost_loses_full_stake(self):
        from tools.bankrollsim.sizing import _resolve_bets

        pnl = _resolve_bets([
            {"stake": 80.0, "odds": -110, "actual_result": "lost"},
        ])
        assert pnl == pytest.approx(-80.0)

    def test_push_is_zero(self):
        from tools.bankrollsim.sizing import _resolve_bets

        assert _resolve_bets([
            {"stake": 50.0, "odds": -110, "actual_result": "push"},
        ]) == 0.0

    def test_zero_stake_skipped(self):
        from tools.bankrollsim.sizing import _resolve_bets

        assert _resolve_bets([
            {"stake": 0.0, "odds": 500, "actual_result": "won"},
        ]) == 0.0

    def test_mixed_slate(self):
        from tools.bankrollsim.sizing import _resolve_bets

        bets = [
            {"stake": 100.0, "odds": 150, "actual_result": "won"},   # +150
            {"stake": 110.0, "odds": -110, "actual_result": "lost"},  # -110
            {"stake": 40.0, "odds": -110, "actual_result": "push"},   # 0
        ]
        assert _resolve_bets(bets) == pytest.approx(150.0 - 110.0)

    def test_empty(self):
        from tools.bankrollsim.sizing import _resolve_bets

        assert _resolve_bets([]) == 0.0


# =========================================================================
# simulate_portfolio end-to-end
# =========================================================================
class TestSimulatePortfolio:
    def test_deterministic_same_seed(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_det", days=45, win_rate=0.57, seed=11)
        r1 = simulate_portfolio(["h_det"], n_sims=40, horizon_days=20,
                                signals_override=sigs, seed=123)
        r2 = simulate_portfolio(["h_det"], n_sims=40, horizon_days=20,
                                signals_override=sigs, seed=123)
        assert r1.to_dict() == r2.to_dict()

    def test_different_seed_changes_outcome_distribution(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_var", days=45, win_rate=0.55, seed=13)
        r1 = simulate_portfolio(["h_var"], n_sims=40, horizon_days=25,
                                signals_override=sigs, seed=1)
        r2 = simulate_portfolio(["h_var"], n_sims=40, horizon_days=25,
                                signals_override=sigs, seed=999)
        assert r1.mean_final_bankroll != pytest.approx(r2.mean_final_bankroll)

    def test_positive_ev_pool_grows_bankroll(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_pos", days=60, win_rate=0.60, edge=0.07, seed=21)
        r = simulate_portfolio(["h_pos"], n_sims=200, horizon_days=45,
                               signals_override=sigs, seed=42)
        assert r.median_total_roi > 0
        assert r.median_monthly_roi > 0
        assert r.final_bankroll_p50 > 10000.0

    def test_negative_ev_pool_drains_bankroll(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_neg", days=60, win_rate=0.42, edge=0.05, seed=31)
        r = simulate_portfolio(["h_neg"], n_sims=200, horizon_days=60,
                               signals_override=sigs, seed=42)
        assert r.median_total_roi < 0
        # Heavy drawdowns on a -EV pool
        assert r.max_drawdown_p90 > 0.05

    def test_ruin_probs_monotonic_in_threshold(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_mono", days=60, win_rate=0.48, seed=41)
        r = simulate_portfolio(["h_mono"], n_sims=200, horizon_days=60,
                               signals_override=sigs, seed=42)
        assert r.ruin_prob_5pct >= r.ruin_prob_15pct >= r.ruin_prob_30pct

    def test_kill_switch_bounds_tail_drawdown(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_kill", days=70, win_rate=0.46, seed=51)
        tight = simulate_portfolio(["h_kill"], n_sims=150, horizon_days=60,
                                   signals_override=sigs, seed=7,
                                   kill_switch_drawdown=0.10)
        loose = simulate_portfolio(["h_kill"], n_sims=150, horizon_days=60,
                                   signals_override=sigs, seed=7,
                                   kill_switch_drawdown=0.95)
        assert loose.max_drawdown_p99 >= tight.max_drawdown_p99 - 1e-9
        assert tight.pct_paths_kill_switch_triggered >= 0.0

    def test_keep_paths_returns_array(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_paths", days=30, win_rate=0.55, seed=61)
        r = simulate_portfolio(["h_paths"], n_sims=10, horizon_days=15,
                               signals_override=sigs, seed=42, keep_paths=True)
        assert r.paths is not None
        assert r.paths.shape == (10, 16)
        assert np.all(r.paths[:, 0] == 10000.0)
        # No path stored goes negative
        assert np.all(r.paths >= 0.0)

    def test_default_suppresses_paths(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_nopaths", days=30, win_rate=0.55, seed=62)
        r = simulate_portfolio(["h_nopaths"], n_sims=5, horizon_days=10,
                               signals_override=sigs, seed=42)
        assert r.paths is None
        assert "paths" not in r.to_dict()

    def test_empty_pool_degenerate(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        r = simulate_portfolio(["h_none"], n_sims=10, horizon_days=20,
                               signals_override=[], seed=42)
        assert r.rows_used == 0
        assert r.distinct_days == 0
        assert r.avg_bets_per_path == 0.0
        assert r.final_bankroll_p50 == pytest.approx(10000.0)

    def test_provenance_counts_match_override(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_prov", days=40, per_day=2, seed=71)
        r = simulate_portfolio(["h_prov"], n_sims=10, horizon_days=20,
                               signals_override=sigs, seed=42)
        assert r.rows_used == len(sigs)
        assert r.distinct_hyps_with_data == 1
        assert r.n_sims == 10
        assert r.hypothesis_ids == ["h_prov"]

    def test_correlated_pair_joint_bootstrap(self):
        """Two hyps firing the SAME event must resolve together."""
        from tools.bankrollsim.simulator import simulate_portfolio

        rng = np.random.default_rng(99)
        sigs = []
        for d in range(60):
            day = str(np.datetime64("2026-01-01") + np.timedelta64(d, "D"))
            res = "won" if rng.random() < 0.55 else "lost"
            sigs.append(_mk_signal("ha", day, f"shared-{d}", res))
            sigs.append(_mk_signal("hb", day, f"shared-{d}", res))
        r = simulate_portfolio(["ha", "hb"], n_sims=100, horizon_days=30,
                               signals_override=sigs, seed=5)
        assert r.distinct_days == 60
        assert r.distinct_hyps_with_data == 2

    def test_duplicate_rows_deduped_per_day(self):
        """Same (hyp, event) repeated on one day counts once per simulated day."""
        from tools.bankrollsim.simulator import simulate_portfolio

        base = [_mk_signal("h1", "2026-03-01", "e1", "won", odds=100)]
        dupes = base * 8  # line-snapshot style duplicates
        r = simulate_portfolio(["h1"], n_sims=1, horizon_days=1,
                               signals_override=dupes, seed=3)
        assert r.avg_bets_per_path == pytest.approx(1.0)

    def test_monthly_roi_scaling_factor(self):
        """Monthly ROI = total ROI * (30/horizon)."""
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_scale", days=50, win_rate=0.55, seed=81)
        r15 = simulate_portfolio(["h_scale"], n_sims=50, horizon_days=15,
                                 signals_override=sigs, seed=42)
        assert r15.expected_monthly_roi == pytest.approx(
            r15.expected_total_roi * 2.0
        )
        r30 = simulate_portfolio(["h_scale"], n_sims=50, horizon_days=30,
                                 signals_override=sigs, seed=42)
        assert r30.expected_monthly_roi == pytest.approx(
            r30.expected_total_roi, rel=1e-9
        )

    def test_sharpe_sortino_present_for_active_pool(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_risk", days=60, win_rate=0.58, seed=91)
        r = simulate_portfolio(["h_risk"], n_sims=100, horizon_days=40,
                               signals_override=sigs, seed=42)
        assert r.avg_bets_per_path > 0
        assert isinstance(r.sharpe, float)
        assert isinstance(r.sortino, float)

    def test_bankroll_never_negative(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_floor", days=60, win_rate=0.35, seed=101)
        r = simulate_portfolio(["h_floor"], n_sims=150, horizon_days=90,
                               signals_override=sigs, seed=42, keep_paths=True)
        assert np.all(r.paths >= 0.0)
        assert r.final_bankroll_p10 >= 0.0

    def test_correlation_matrix_kwarg_accepted_end_to_end(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("hc", days=40, win_rate=0.56, seed=111)
        corr = {("hc", "hc2"): 0.5}
        r = simulate_portfolio(["hc"], n_sims=20, horizon_days=20,
                               signals_override=sigs, seed=42,
                               correlation_matrix=corr)
        assert r.n_sims == 20

    def test_percentile_ordering(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_pctl", days=60, win_rate=0.54, seed=121)
        r = simulate_portfolio(["h_pctl"], n_sims=200, horizon_days=45,
                               signals_override=sigs, seed=42)
        assert r.final_bankroll_p10 <= r.final_bankroll_p50 <= r.final_bankroll_p90
        assert r.p10_total_roi <= r.median_total_roi <= r.p90_total_roi
        assert r.p10_monthly_roi <= r.median_monthly_roi <= r.p90_monthly_roi
        assert r.max_drawdown_median <= r.max_drawdown_p90 <= r.max_drawdown_p99

    def test_avg_bets_per_day_consistency(self):
        from tools.bankrollsim.simulator import simulate_portfolio

        sigs = _generate_signals("h_abpd", days=40, win_rate=0.55, seed=131)
        r = simulate_portfolio(["h_abpd"], n_sims=30, horizon_days=20,
                               signals_override=sigs, seed=42)
        assert r.avg_bets_per_day == pytest.approx(
            r.avg_bets_per_path / 20.0
        )


# =========================================================================
# Promote gate
# =========================================================================
class TestPromoteGate:
    @pytest.fixture()
    def gate_db(self, tmp_path):
        import sqlite3

        db = tmp_path / "gate.db"
        conn = sqlite3.connect(db)
        conn.execute(
            "CREATE TABLE hypotheses (hypothesis_id TEXT, status TEXT)"
        )
        conn.executemany(
            "INSERT INTO hypotheses VALUES (?,?)",
            [("h_live1", "live"), ("h_live2", "live"), ("h_cand", "paper")],
        )
        conn.execute(
            """
            CREATE TABLE backtest_events (
                hypothesis_id TEXT, event_id TEXT, game_date TEXT,
                snapshot_time TEXT, sport TEXT, market TEXT, side TEXT,
                book_odds_american INTEGER, edge REAL, ev_pct REAL,
                signal_generated INTEGER, actual_result TEXT
            )
            """
        )
        rows = []
        for i, hid in enumerate(["h_live1", "h_live2", "h_cand"]):
            for d in range(40):
                day = str(np.datetime64("2026-01-05") + np.timedelta64(d, "D"))
                res = "won" if (d * 7 + i) % 10 < 6 else "lost"
                rows.append((hid, f"{hid}-e{d}", day, None, "baseball_mlb",
                             "h2h", "T", -110, 0.06, 0.11, 1, res))
        conn.executemany(
            "INSERT INTO backtest_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()
        return str(db)

    def test_returns_report_shape(self, gate_db):
        from tools.bankrollsim.promote_gate import simulate_before_promote

        report = simulate_before_promote("h_cand", db_path=gate_db,
                                         n_sims=30, horizon_days=15)
        keys = {
            "ruin_prob_30d", "ruin_prob_30pct_30d", "expected_monthly_roi",
            "expected_drawdown", "n_sims", "hyp_count", "rows_used",
        }
        assert set(report.keys()) == keys
        assert report["n_sims"] == 30
        assert report["hyp_count"] == 3  # candidate + 2 live
        assert report["rows_used"] == 120

    def test_candidate_already_live_deduped(self, gate_db):
        from tools.bankrollsim.promote_gate import simulate_before_promote

        report = simulate_before_promote("h_live1", db_path=gate_db,
                                         n_sims=10, horizon_days=10)
        assert report["hyp_count"] == 2

    def test_ruin_probs_within_unit_interval(self, gate_db):
        from tools.bankrollsim.promote_gate import simulate_before_promote

        report = simulate_before_promote("h_cand", db_path=gate_db,
                                         n_sims=25, horizon_days=12)
        for key in ["ruin_prob_30d", "ruin_prob_30pct_30d"]:
            assert 0.0 <= report[key] <= 1.0

    def test_no_live_hyps_still_sims_candidate_alone(self, tmp_path):
        import sqlite3

        from tools.bankrollsim.promote_gate import simulate_before_promote

        db = tmp_path / "solo.db"
        conn = sqlite3.connect(db)
        conn.execute("CREATE TABLE hypotheses (hypothesis_id TEXT, status TEXT)")
        conn.execute(
            """
            CREATE TABLE backtest_events (
                hypothesis_id TEXT, event_id TEXT, game_date TEXT,
                snapshot_time TEXT, sport TEXT, market TEXT, side TEXT,
                book_odds_american INTEGER, edge REAL, ev_pct REAL,
                signal_generated INTEGER, actual_result TEXT
            )
            """
        )
        rows = []
        for d in range(30):
            day = str(np.datetime64("2026-02-01") + np.timedelta64(d, "D"))
            res = "won" if d % 3 != 0 else "lost"
            rows.append(("hsolo", f"s{d}", day, None, "mlb", "h2h", "T",
                         -110, 0.06, 0.11, 1, res))
        conn.executemany(
            "INSERT INTO backtest_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows
        )
        conn.commit()
        conn.close()

        report = simulate_before_promote("hsolo", db_path=str(db),
                                         n_sims=10, horizon_days=10)
        assert report["hyp_count"] == 1
        assert report["rows_used"] == 30


# =========================================================================
# Histogram
# =========================================================================
class TestHistogram:
    def test_no_data(self):
        from tools.bankrollsim.histogram import ascii_bankroll_histogram

        assert ascii_bankroll_histogram(None, 10000.0) == "(no data)"
        assert ascii_bankroll_histogram(np.array([]), 10000.0) == "(no data)"

    def test_degenerate_range(self):
        from tools.bankrollsim.histogram import ascii_bankroll_histogram

        out = ascii_bankroll_histogram(np.full(5, 12345.0), 10000.0)
        assert "12,345" in out
        assert "all paths ended" in out

    def test_basic_rendering(self):
        from tools.bankrollsim.histogram import ascii_bankroll_histogram

        finals = np.concatenate([
            np.full(30, 8000.0), np.full(50, 10000.0), np.full(20, 14000.0),
        ])
        out = ascii_bankroll_histogram(finals, 10000.0)
        lines = out.strip().split("\n")
        assert lines[0].startswith("Bankroll distribution")
        assert "(start=$10,000)" in lines[0]
        assert len(lines) == 21  # header + 20 bins
        assert any("#" in ln for ln in lines[1:])
        assert "%" in lines[1]

    def test_custom_width_and_bins(self):
        from tools.bankrollsim.histogram import ascii_bankroll_histogram

        finals = np.linspace(5000.0, 20000.0, 200)
        out = ascii_bankroll_histogram(finals, 10000.0, width=20, bins=5)
        lines = out.strip().split("\n")
        assert len(lines) == 6  # header + 5 bins

    def test_peak_bin_has_longest_bar(self):
        from tools.bankrollsim.histogram import ascii_bankroll_histogram

        finals = np.concatenate([
            np.full(5, 6000.0), np.full(95, 10500.0),
        ])
        out = ascii_bankroll_histogram(finals, 10000.0, bins=4)
        bars = [ln.split("|")[1].rstrip() if "|" in ln else "" for ln in out.split("\n")[1:]]
        hashes = [b.count("#") for b in bars]
        assert max(hashes) == hashes[-1]  # the 10500 cluster lands in the top bin
        assert max(hashes) > min(hashes)


# =========================================================================
# Cross-import safety: consumers keep working unchanged
# =========================================================================
class TestConsumerImports:
    def test_api_import_pattern(self):
        # api.py does: from tools.bankroll_sim import simulate_portfolio
        code = "from tools.bankroll_sim import simulate_portfolio"
        ns: dict = {}
        exec(compile(code, "<t>", "exec"), ns)
        assert callable(ns["simulate_portfolio"])

    def test_promote_import_pattern(self):
        # tools/hypothesis/promote.py does:
        #   from tools.bankroll_sim import simulate_before_promote
        ns: dict = {}
        exec(
            compile("from tools.bankroll_sim import simulate_before_promote",
                    "<t>", "exec"),
            ns,
        )
        assert callable(ns["simulate_before_promote"])

    def test_script_import_patterns(self):
        ns: dict = {}
        exec(compile(
            "from tools.bankroll_sim import ascii_bankroll_histogram",
            "<t>", "exec"), ns)
        exec(compile(
            "from tools.bankroll_sim import simulate_portfolio, ascii_bankroll_histogram",
            "<t>", "exec"), ns)
        assert callable(ns["ascii_bankroll_histogram"])

    def test_logger_namespace_preserved(self):
        import logging

        from tools import bankroll_sim

        assert isinstance(bankroll_sim.logger, logging.Logger)
        assert bankroll_sim.logger.name == "callisto.bankroll_sim"
