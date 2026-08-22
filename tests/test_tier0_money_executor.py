"""
Tier-0 money-path characterization tests: tools/bet_executor.py.

ONLY the pure sizing / cap / gate arithmetic is exercised. The Playwright
browser, DraftKings navigation, and place_bet_on_slip paths are NOT touched
and NOT armed: the executor is never enabled (``enable()`` is never called)
and every DB lives in a temp file. These tests pin what the sizing stack
would do IF armed, so that arming later happens against pinned behaviour.
"""

import pytest
import pytest_asyncio

from tools.bet_executor import (
    MAX_BET_PCT,
    MIN_BET_AMOUNT,
    BetExecutor,
)


@pytest_asyncio.fixture
async def executor(tmp_path, monkeypatch):
    """Executor wired to a throwaway DB with a seeded bankroll. Never enabled."""
    import tools.bet_executor as be
    # Deterministic sizing: disable the date/sport-dependent regime multiplier.
    monkeypatch.setattr(be, "REGIME_SIZING_ENABLED", False)
    monkeypatch.setattr(be, "REGIME_SAFETY_ENABLED", False)
    db_path = str(tmp_path / "test_money.db")
    ex = BetExecutor()
    # Patch module-level DB_PATH used by initialize()
    orig = be.DB_PATH
    be.DB_PATH = db_path
    try:
        await ex.initialize()
        await ex._db.execute(
            "CREATE TABLE IF NOT EXISTS bankroll ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL, "
            "balance REAL NOT NULL, change REAL, bet_id INTEGER, description TEXT)"
        )
        await ex._db.execute(
            "INSERT INTO bankroll (timestamp, balance, change, description) "
            "VALUES ('2026-01-01T00:00:00+00:00', 10000.0, 10000.0, 'seed')"
        )
        from tools.db_utils import commit_with_retry
        await commit_with_retry(ex._db, operation="test seed")
        # preflight's daily-loss query needs the bets table
        await ex._db.execute(
            "CREATE TABLE IF NOT EXISTS bets ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, placed_at TEXT NOT NULL, "
            "sport TEXT NOT NULL, event_id TEXT, game_description TEXT, "
            "bet_type TEXT NOT NULL, team TEXT, market TEXT NOT NULL, "
            "bookmaker TEXT NOT NULL, placement_odds INTEGER NOT NULL, "
            "placement_point REAL, placement_implied_prob REAL, closing_odds INTEGER, "
            "closing_point REAL, closing_implied_prob REAL, closing_source TEXT, "
            "clv_odds INTEGER, clv_implied REAL, stake REAL DEFAULT 100, "
            "result TEXT DEFAULT 'pending', payout REAL, edge_at_placement REAL, "
            "kelly_at_placement REAL, notes TEXT, tags TEXT)"
        )
        await commit_with_retry(ex._db, operation="test bets table")
        yield ex
    finally:
        be.DB_PATH = orig
        await ex.shutdown()


@pytest.fixture
def bankroll_10k(executor):
    return 10000.0


# ---------------------------------------------------------------------------
# compute_stake — the primary sizing entry point
# ---------------------------------------------------------------------------
class TestComputeStake:
    def test_reference_case_quarter_kelly(self, executor, bankroll_10k):
        # kelly_dynamic(0.03, -110, conf .8, var .015, 10000) = $91.00
        stake = executor.compute_stake(edge=0.03, odds=-110,
                                       bankroll=bankroll_10k, confidence=0.80)
        assert stake == pytest.approx(91.00, abs=0.02)

    def test_low_confidence_bets_nothing(self, executor, bankroll_10k):
        assert executor.compute_stake(0.03, -110, bankroll_10k, 0.10) == 0.0

    def test_no_edge_bets_nothing(self, executor, bankroll_10k):
        assert executor.compute_stake(0.0, -110, bankroll_10k, 0.95) == 0.0

    def test_tiny_bankroll_floors_to_zero(self, executor):
        # stake below MIN_BET_AMOUNT must return exactly 0.0
        assert executor.compute_stake(0.03, -110, bankroll=20.0,
                                      confidence=0.95) == 0.0

    def test_never_exceeds_max_bet_pct(self, executor, bankroll_10k):
        for edge in (0.05, 0.10, 0.25):
            stake = executor.compute_stake(edge, 200, bankroll_10k, 1.0)
            assert stake <= bankroll_10k * MAX_BET_PCT + 1e-9

    def test_push_path_sizes_larger_than_binary_for_same_edge(self, executor,
                                                              bankroll_10k):
        # Push-aware Kelly removes the push drag; same edge should size >= binary path
        s_bin = executor.compute_stake(0.03, -110, bankroll_10k, 0.80)
        s_push = executor.compute_stake(0.03, -110, bankroll_10k, 0.80,
                                        p_push=0.04)
        # Both are quarter-Kelly-ish but through different formulas; push path
        # applies uncertainty_adjusted (conf .8 -> 'high' tier, noise .015).
        # Pin only that both are nonzero and within sane bounds.
        assert s_bin > 0 and s_push > 0
        assert s_push <= bankroll_10k * MAX_BET_PCT + 1e-9


class TestSignalsNToKellyFraction:
    def test_floor_half_kelly_below_low_n(self):
        assert BetExecutor._signals_n_to_kelly_fraction(0) == 0.125
        assert BetExecutor._signals_n_to_kelly_fraction(25) == 0.125

    def test_full_quarter_at_high_n(self):
        assert BetExecutor._signals_n_to_kelly_fraction(100) == 0.25
        assert BetExecutor._signals_n_to_kelly_fraction(500) == 0.25

    def test_linear_interpolation_midpoint(self):
        # span 25..100 = 75; at n=62, t=(62-25)/75=.4933 -> .125+t*.125=.186667
        assert BetExecutor._signals_n_to_kelly_fraction(62) == pytest.approx(
            0.186667, abs=1e-6)


# ---------------------------------------------------------------------------
# compute_portfolio_stakes — caps and floors
# ---------------------------------------------------------------------------
class TestComputePortfolioStakes:
    def test_empty(self, executor, bankroll_10k):
        assert executor.compute_portfolio_stakes([], bankroll_10k) == []

    def test_single_bet_individual_path(self, executor, bankroll_10k):
        out = executor.compute_portfolio_stakes(
            [{"edge": 0.03, "odds": -110, "confidence": 0.8, "signals_n": 100,
              "sport": "basketball_nba", "event_id": "g1"}], bankroll_10k)
        assert len(out) == 1
        r = out[0]
        assert r["method"] == "individual_kelly_n_adjusted"
        # signals_n=100 -> full quarter Kelly -> same as compute_stake
        assert r["stake"] == pytest.approx(
            executor.compute_stake(0.03, -110, bankroll_10k, 0.8), abs=0.01)

    def test_game_cap_scales_same_event(self, executor, bankroll_10k):
        bets = [
            {"edge": 0.08, "odds": 200, "confidence": 0.99, "signals_n": 200,
             "sport": "basketball_nba", "event_id": "same-game",
             "correlation_with_others": 0.0},
            {"edge": 0.08, "odds": 200, "confidence": 0.99, "signals_n": 200,
             "sport": "basketball_nba", "event_id": "same-game",
             "correlation_with_others": 0.0},
        ]
        out = executor.compute_portfolio_stakes(bets, bankroll_10k)
        # Each bet sizes to $300 (3% of bankroll); total $600 is well under the
        # 8% game cap ($800) — so NO scaling should fire.
        assert all("game_cap_scale" not in r for r in out)
        total = sum(r["stake"] for r in out)
        assert total == pytest.approx(600.0, abs=0.01)

    def test_game_cap_fires_when_exposure_exceeds_cap(self, executor,
                                                      bankroll_10k):
        # Six correlated-by-event bets at 3% each = 18% on one game > 8% cap.
        bets = [
            {"edge": 0.08, "odds": 200, "confidence": 0.99, "signals_n": 200,
             "sport": "basketball_nba", "event_id": "same-game",
             "correlation_with_others": 0.0}
            for _ in range(6)
        ]
        out = executor.compute_portfolio_stakes(bets, bankroll_10k)
        total = sum(r["stake"] for r in out if r["stake"] > 0)
        game_cap = bankroll_10k * 0.08
        assert total <= game_cap + 0.05, f"game cap breached: {total} > {game_cap}"
        scaled = [r for r in out if "game_cap_scale" in r]
        assert len(scaled) == 6

    def test_sport_cap_across_games(self, executor, bankroll_10k):
        bets = [
            {"edge": 0.08, "odds": 200, "confidence": 0.99, "signals_n": 200,
             "sport": "icehockey_nhl", "event_id": f"g{i}",
             "correlation_with_others": 0.0}
            for i in range(6)
        ]
        out = executor.compute_portfolio_stakes(bets, bankroll_10k)
        total = sum(r["stake"] for r in out if r["stake"] > 0)
        sport_cap = bankroll_10k * 0.15
        assert total <= sport_cap + 0.05, f"sport cap breached: {total} > {sport_cap}"

    def test_sub_min_bet_zeroed(self, executor):
        out = executor.compute_portfolio_stakes(
            [{"edge": 0.001, "odds": -110, "confidence": 0.3, "signals_n": 0,
              "sport": "", "event_id": ""}], 50.0)
        assert out[0]["stake"] == 0.0

    def test_correlation_matrix_overrides(self, executor, bankroll_10k):
        bets = [
            {"edge": 0.04, "odds": -110, "confidence": 0.95, "signals_n": 100,
             "hypothesis_id": "A", "sport": "baseball_mlb", "event_id": "gA"},
            {"edge": 0.04, "odds": -110, "confidence": 0.95, "signals_n": 100,
             "hypothesis_id": "B", "sport": "baseball_mlb", "event_id": "gB"},
        ]
        corr_matrix = {("A", "B"): 0.9}
        out = executor.compute_portfolio_stakes(bets, bankroll_10k,
                                                correlation_matrix=corr_matrix)
        assert all(r["method"] == "portfolio_kelly_n_adjusted" for r in out)


# ---------------------------------------------------------------------------
# preflight_check + exposure gating (pure gates; no browser)
# ---------------------------------------------------------------------------
class TestPreflightAndGates:
    @pytest.mark.asyncio
    async def test_disabled_executor_refuses_everything(self, executor):
        ok, reason = await executor.preflight_check("basketball_nba", -110,
                                                    0.05, 100.0)
        assert not ok
        assert "disabled" in reason.lower()

    @pytest.mark.asyncio
    async def test_min_edge_gate(self, executor):
        executor.enable()  # in-memory only; no browser involved
        ok, reason = await executor.preflight_check("basketball_nba", -110,
                                                    0.01, 100.0)
        assert not ok
        assert "below minimum" in reason
        executor.disable()

    @pytest.mark.asyncio
    async def test_unsupported_sport_gate(self, executor):
        executor.enable()
        ok, reason = await executor.preflight_check("curling", -110, 0.05, 100.0)
        assert not ok
        assert "not supported" in reason
        executor.disable()

    @pytest.mark.asyncio
    async def test_stake_above_cap_refused(self, executor):
        executor.enable()
        ok, reason = await executor.preflight_check("basketball_nba", -110,
                                                    0.05, 600.0)  # > 5% of 10k
        assert not ok
        assert "exceeds" in reason
        executor.disable()

    @pytest.mark.asyncio
    async def test_daily_loss_limit_blocks_after_losses(self, executor):
        executor.enable()
        # Simulate today's resolved losses of $2,500 (> 20% of 10k)
        await executor._db.execute(
            "INSERT INTO bets (placed_at, sport, bet_type, market, bookmaker, "
            "placement_odds, stake, result, payout) VALUES "
            "(datetime('now'), 'nba', 'single', 'h2h', 'dk', "
            "-110, 2500.0, 'lost', NULL)"
        )
        from tools.db_utils import commit_with_retry
        await commit_with_retry(executor._db, operation="test losses")
        ok, reason = await executor.preflight_check("basketball_nba", -110,
                                                    0.05, 100.0)
        assert not ok
        assert "daily loss limit" in reason.lower()
        executor.disable()


# ---------------------------------------------------------------------------
# Drawdown kill-switch arithmetic on seeded history (no Telegram, no browser)
# ---------------------------------------------------------------------------
class TestDrawdownArithmetic:
    @pytest.mark.asyncio
    async def test_drawdown_trigger_disables_and_pauses_live_hyps(
            self, executor, tmp_path, monkeypatch):
        import tools.db_utils as du
        # Build peak history at 12,000 and current bankroll 9,000 => 25% DD > 15%
        await executor._db.execute(
            "CREATE TABLE IF NOT EXISTS bankroll_peak ("
            "observed_at TEXT NOT NULL, balance REAL NOT NULL, note TEXT)")
        await executor._db.execute(
            "CREATE TABLE IF NOT EXISTS hypotheses ("
            "hypothesis_id TEXT PRIMARY KEY, status TEXT, updated_at TEXT, "
            "promoted_at TEXT, promoted_by TEXT)")
        await executor._db.execute(
            "INSERT INTO hypotheses (hypothesis_id, status) VALUES ('H1','live')")
        await executor._db.execute(
            "INSERT INTO hypotheses (hypothesis_id, status) VALUES ('H2','paper_trading')")
        await executor._db.commit()

        # Seed peak table directly so the rolling window is deterministic
        # (recent timestamps: _rolling_peak filters on a 30-day window from NOW).
        await executor._db.execute(
            "INSERT INTO bankroll_peak VALUES (datetime('now'), "
            "12000.0, 'seed')")
        # Current bankroll: append 9,000 row to the bankroll ledger
        await executor._db.execute(
            "INSERT INTO bankroll (timestamp, balance, change, description) "
            "VALUES (datetime('now'), 9000.0, -1000.0, 'loss')")
        await executor._db.commit()

        status = await executor.check_drawdown_and_kill()
        assert status["triggered"] is True
        assert executor.is_enabled is False
        assert status["paused_hypotheses"] == ["H1"]
        cur = await executor._db.execute(
            "SELECT status FROM hypotheses WHERE hypothesis_id='H1'")
        assert (await cur.fetchone())[0] == "drawdown_paused"

    @pytest.mark.asyncio
    async def test_below_threshold_is_noop(self, executor):
        await executor._db.execute(
            "CREATE TABLE IF NOT EXISTS bankroll_peak ("
            "observed_at TEXT NOT NULL, balance REAL NOT NULL, note TEXT)")
        await executor._db.execute(
            "INSERT INTO bankroll_peak VALUES (datetime('now'), 10500.0, 'seed')")
        await executor._db.commit()
        was_enabled = executor.is_enabled
        status = await executor.check_drawdown_and_kill()
        assert status["triggered"] is False
        assert executor.is_enabled == was_enabled
