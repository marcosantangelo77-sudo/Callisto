"""Tests for feat/bet-execution-hardening (2026-04-23).

Covers the bet-execution-path safety net:
  1. Kelly math with known-answer inputs (fractional, push-aware, dynamic).
  2. Correlation-adjusted portfolio sizing using an explicit matrix.
  3. Idempotency: posting the same bet twice returns the same bet_id.
  4. Each circuit breaker individually fires and blocks a placement.
  5. Odds-conversion edge cases (0, -100, +100, extreme longshots).
  6. Devig symmetry for 2-way and non-negative probs for 3-way.

Nothing here touches a real sportsbook — the Playwright surface is
bypassed via ``preflight_check`` and direct DB writes.
"""

import asyncio
import os
from datetime import datetime, timedelta, timezone

import aiosqlite
import pytest


# Pin all risk caps to known values before importing the modules under test.
os.environ["EXECUTOR_MAX_BET_PCT"] = "0.05"
os.environ["EXECUTOR_MAX_OPEN_EXPOSURE_PCT"] = "0.25"
os.environ["EXECUTOR_DAILY_LOSS_PCT"] = "0.20"
os.environ["EXECUTOR_MIN_EDGE"] = "0.02"
os.environ["EXECUTOR_LIVE_MIN_EDGE"] = "0.03"
os.environ["EXECUTOR_KELLY_FRACTION"] = "0.25"
os.environ["EXECUTOR_MIN_BET"] = "1.00"
os.environ["CALLISTO_MAX_DAILY_RISK_PCT"] = "0.30"
os.environ["CALLISTO_MAX_GAME_EXPOSURE_PCT"] = "0.08"
os.environ["CALLISTO_MAX_SPORT_EXPOSURE_PCT"] = "0.15"
os.environ["CALLISTO_MAX_DRAWDOWN_PCT"] = "0.15"
os.environ["CALLISTO_DRAWDOWN_WINDOW_DAYS"] = "30"
os.environ["CALLISTO_REGIME_SIZING"] = "0"
os.environ["CALLISTO_REGIME_SAFETY"] = "0"


# =============================================================================
# Section 1 — Kelly math with known-answer inputs
# =============================================================================

class TestKellyMath:
    """Known-answer tests. If any of these drift, sizing is wrong."""

    def test_kelly_binary_plus_ev_matches_closed_form(self):
        """f* = (bp - q) / b.
        p=0.55, decimal=2.10 -> b=1.10, q=0.45 -> (1.10*0.55 - 0.45)/1.10 = 0.1409..."""
        from tools.sizing import kelly_binary
        f = kelly_binary(fair_prob=0.55, decimal_odds=2.10)
        assert abs(f - 0.14090909) < 1e-6, f"got {f}"

    def test_kelly_binary_no_edge_returns_zero(self):
        """Fair prob == implied -> zero wager."""
        from tools.sizing import kelly_binary
        # At decimal=2.0, implied is 0.5. No edge -> Kelly = 0.
        f = kelly_binary(fair_prob=0.5, decimal_odds=2.0)
        assert f == 0.0

    def test_kelly_binary_negative_ev_clamped_to_zero(self):
        """f* is negative for -EV bets; Kelly clamps to 0."""
        from tools.sizing import kelly_binary
        f = kelly_binary(fair_prob=0.40, decimal_odds=2.10)
        assert f == 0.0

    def test_kelly_with_push_matches_closed_form(self):
        """p_win=0.54, p_push=0.04, decimal=1.909 -> close to 0.078."""
        from tools.sizing import kelly_with_push
        f = kelly_with_push(p_win=0.54, p_push=0.04, decimal_odds=1.909)
        # p_loss = 0.42; b = 0.909; (0.909*0.54 - 0.42) / 0.909 = 0.07788...
        assert abs(f - 0.07788) < 1e-3, f"got {f}"

    def test_kelly_fractional_quarter_reduces_full(self):
        """quarter-Kelly == full-Kelly / 4."""
        from tools.kelly import kelly_full, kelly_fractional
        full = kelly_full(edge=0.05, odds=-110)
        quarter = kelly_fractional(edge=0.05, odds=-110, fraction=0.25)
        assert abs(quarter - full * 0.25) < 1e-4

    def test_kelly_dynamic_caps_at_5pct(self):
        """Hard cap: no single bet exceeds 5% of bankroll regardless of edge."""
        from tools.kelly import kelly_dynamic
        r = kelly_dynamic(
            edge=0.50, odds=+500, confidence_score=0.95,
            variance_estimate=0.01, bankroll=10_000.0,
        )
        assert r["fraction"] <= 0.05 + 1e-6
        assert r["stake"] <= 500.00 + 0.01

    def test_kelly_dynamic_unverified_confidence_zero_stake(self):
        """Score < 0.30 = UNVERIFIED tier -> 0.0 multiplier -> no bet."""
        from tools.kelly import kelly_dynamic
        r = kelly_dynamic(
            edge=0.05, odds=-110, confidence_score=0.20,
            variance_estimate=0.02, bankroll=10_000.0,
        )
        assert r["stake"] == 0.0
        assert r["tier"] == "UNVERIFIED"


# =============================================================================
# Section 2 — Correlation-adjusted portfolio sizing with matrix
# =============================================================================

class TestPortfolioCorrelation:
    """Verify the correlation matrix actually enters the sizing path."""

    @staticmethod
    def _hyp(i: int, event: str, sport: str = "baseball_mlb") -> dict:
        return {
            "edge": 0.04,
            "odds": -110,
            "confidence": 0.80,
            "event_id": event,
            "sport": sport,
            "market_type": "h2h",
            "side": f"side_{i}",
            "hypothesis_id": f"hyp_{i}",
            "description": f"hyp_{i}",
            "signals_n": 150,
        }

    def test_zero_correlation_allocates_more_than_full_correlation(self):
        """Same edges + same bankroll; rho=0.0 -> higher total than rho=1.0."""
        from tools.bet_executor import BetExecutor
        executor = BetExecutor()
        bets = [self._hyp(i, f"evt_{i}") for i in range(4)]
        ids = [b["hypothesis_id"] for b in bets]
        corr_zero = {(ids[i], ids[j]): 0.0
                     for i in range(len(ids)) for j in range(i + 1, len(ids))}
        corr_one = {(ids[i], ids[j]): 1.0
                    for i in range(len(ids)) for j in range(i + 1, len(ids))}
        sized_zero = executor.compute_portfolio_stakes(
            bets=bets, bankroll=10_000.0, correlation_matrix=corr_zero,
        )
        sized_one = executor.compute_portfolio_stakes(
            bets=bets, bankroll=10_000.0, correlation_matrix=corr_one,
        )
        total_zero = sum(r["stake"] for r in sized_zero)
        total_one = sum(r["stake"] for r in sized_one)
        assert total_zero > total_one, (
            f"rho=0 total ${total_zero:.2f} must exceed rho=1 total ${total_one:.2f}"
        )

    def test_correlation_reported_in_results(self):
        """Each sized bet must carry the correlation that was applied."""
        from tools.bet_executor import BetExecutor
        executor = BetExecutor()
        bets = [self._hyp(i, f"evt_{i}") for i in range(3)]
        ids = [b["hypothesis_id"] for b in bets]
        corr = {(ids[i], ids[j]): 0.5
                for i in range(len(ids)) for j in range(i + 1, len(ids))}
        sized = executor.compute_portfolio_stakes(
            bets=bets, bankroll=10_000.0, correlation_matrix=corr,
        )
        # At least one row must have non-default correlation (default is 0.1).
        assert any(abs(r.get("correlation", 0.0) - 0.5) < 0.2 for r in sized)

    def test_batch_dedup_drops_same_event_market_side(self):
        """Two signals with identical (event, market_type, side) -> one position."""
        from tools.bet_executor import BetExecutor
        executor = BetExecutor()
        # Two bets with the same key (event_id + market_type + side)
        dup_a = self._hyp(0, "evt_shared")
        dup_b = self._hyp(1, "evt_shared")
        dup_b["side"] = dup_a["side"]  # force duplicate
        distinct = self._hyp(2, "evt_other")
        sized = executor.compute_portfolio_stakes(
            bets=[dup_a, dup_b, distinct],
            bankroll=10_000.0,
            correlation_matrix={},
        )
        # One of the duplicates should be silently dropped; result size = 2.
        assert len(sized) == 2, f"got {len(sized)} rows: {sized}"


# =============================================================================
# Section 3 — Idempotency for /bets/record
# =============================================================================

async def _mk_clv_tracker(tmp_path):
    """Spin up a CLVTracker against a fresh file-backed DB."""
    from tools.clv_tracker import CLVTracker
    db_path = tmp_path / "clv.db"
    tracker = CLVTracker(db_path=str(db_path))
    await tracker.initialize()
    return tracker


class TestIdempotency:
    @pytest.mark.asyncio
    async def test_external_id_dedup_returns_same_bet_id(self, tmp_path):
        """Same external_id -> same bet_id."""
        tracker = await _mk_clv_tracker(tmp_path)
        try:
            bid_a = await tracker.record_bet(
                sport="baseball_mlb", game_description="Yankees @ Red Sox",
                team="Yankees", market="h2h", bookmaker="draftkings",
                placement_odds=-150, stake=100.0, event_id="evt_1",
                external_id="signal_abc_123",
            )
            bid_b = await tracker.record_bet(
                sport="baseball_mlb", game_description="Yankees @ Red Sox",
                team="Yankees", market="h2h", bookmaker="draftkings",
                placement_odds=-150, stake=100.0, event_id="evt_1",
                external_id="signal_abc_123",
            )
            assert bid_a == bid_b, "same external_id must map to same bet_id"

            # Confirm only one row was inserted.
            cur = await tracker._db.execute(
                "SELECT COUNT(*) FROM bets WHERE tags LIKE '%ext:signal_abc_123%'"
            )
            row = await cur.fetchone()
            assert row[0] == 1
        finally:
            await tracker.close()

    @pytest.mark.asyncio
    async def test_fingerprint_dedup_without_external_id(self, tmp_path):
        """Without external_id, identical (event/team/market/book/odds/stake)
        within the same hour must dedup."""
        tracker = await _mk_clv_tracker(tmp_path)
        try:
            bid_a = await tracker.record_bet(
                sport="nba", game_description="Lakers @ Celtics",
                team="Lakers", market="h2h", bookmaker="fanatics",
                placement_odds=+150, stake=50.0, event_id="evt_fp",
            )
            bid_b = await tracker.record_bet(
                sport="nba", game_description="Lakers @ Celtics",
                team="Lakers", market="h2h", bookmaker="fanatics",
                placement_odds=+150, stake=50.0, event_id="evt_fp",
            )
            assert bid_a == bid_b
        finally:
            await tracker.close()

    @pytest.mark.asyncio
    async def test_different_external_id_inserts_fresh_row(self, tmp_path):
        tracker = await _mk_clv_tracker(tmp_path)
        try:
            bid_a = await tracker.record_bet(
                sport="baseball_mlb", game_description="A @ B",
                team="A", market="h2h", bookmaker="draftkings",
                placement_odds=-110, stake=100.0, event_id="e",
                external_id="alpha",
            )
            bid_b = await tracker.record_bet(
                sport="baseball_mlb", game_description="A @ B",
                team="A", market="h2h", bookmaker="draftkings",
                placement_odds=-110, stake=200.0, event_id="e",
                external_id="beta",  # different id, different stake
            )
            assert bid_a != bid_b
        finally:
            await tracker.close()


# =============================================================================
# Section 4 — Circuit breakers fire
# =============================================================================

async def _mk_executor_with_bankroll(tmp_path, starting: float = 10_000.0):
    """Minimal executor with bets + bankroll tables + seeded balance."""
    from tools.bet_executor import BetExecutor
    db_path = tmp_path / "exec.db"
    db = await aiosqlite.connect(str(db_path))
    await db.execute("""
        CREATE TABLE bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT NOT NULL,
            balance REAL NOT NULL,
            change REAL,
            bet_id INTEGER,
            description TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE bankroll_peak (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            observed_at DATETIME NOT NULL,
            balance REAL NOT NULL,
            note TEXT
        )
    """)
    await db.execute("""
        CREATE TABLE bets (
            bet_id INTEGER PRIMARY KEY AUTOINCREMENT,
            placed_at TEXT,
            sport TEXT,
            event_id TEXT,
            stake REAL,
            result TEXT,
            payout REAL
        )
    """)
    await db.execute(
        "INSERT INTO bankroll (timestamp, balance, change, description) VALUES (?, ?, 0, 'seed')",
        (datetime.now(timezone.utc).isoformat(), starting),
    )
    await db.commit()
    executor = BetExecutor()
    executor._db = db
    executor._enabled = True
    return executor, db


class TestCircuitBreakers:
    @pytest.mark.asyncio
    async def test_executor_disabled_blocks_placement(self, tmp_path):
        executor, db = await _mk_executor_with_bankroll(tmp_path)
        try:
            executor._enabled = False
            ok, reason = await executor.preflight_check(
                sport="baseball_mlb", odds=-110, edge=0.05, stake=50.0,
            )
            assert not ok
            assert "executor_disabled" in reason
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_live_min_edge_blocks_below_threshold(self, tmp_path):
        executor, db = await _mk_executor_with_bankroll(tmp_path)
        try:
            ok, reason = await executor.preflight_check(
                sport="baseball_mlb", odds=-110, edge=0.025, stake=50.0,
            )
            assert not ok
            assert "live_min_edge" in reason, reason
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_max_single_bet_cap(self, tmp_path):
        executor, db = await _mk_executor_with_bankroll(tmp_path)
        try:
            # 10k bankroll * 5% = 500 cap. Stake 501 -> blocked.
            ok, reason = await executor.preflight_check(
                sport="baseball_mlb", odds=-110, edge=0.05, stake=501.0,
            )
            assert not ok
            assert "max_single_bet_pct" in reason
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_daily_risk_cap(self, tmp_path):
        executor, db = await _mk_executor_with_bankroll(tmp_path)
        try:
            # Seed 2999 of stakes today; cap is 30% of 10k = 3000.
            today_iso = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO bets (placed_at, sport, stake, result) VALUES (?, 'mlb', 2999.0, 'pending')",
                (today_iso,),
            )
            await db.commit()
            # Any new stake >= 2 should exceed the daily cap.
            ok, reason = await executor.preflight_check(
                sport="baseball_mlb", odds=-110, edge=0.05, stake=2.0,
            )
            assert not ok
            assert "daily_risk" in reason, reason
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_daily_loss_cap(self, tmp_path):
        executor, db = await _mk_executor_with_bankroll(tmp_path)
        try:
            # Seed a lost bet of 2100 today; cap is 20% of 10k = 2000.
            today_iso = datetime.now(timezone.utc).isoformat()
            await db.execute(
                "INSERT INTO bets (placed_at, sport, stake, result) VALUES (?, 'mlb', 2100.0, 'lost')",
                (today_iso,),
            )
            await db.commit()
            ok, reason = await executor.preflight_check(
                sport="baseball_mlb", odds=-110, edge=0.05, stake=10.0,
            )
            assert not ok
            assert "daily_loss" in reason, reason
        finally:
            await db.close()

    @pytest.mark.asyncio
    async def test_drawdown_kill_flips_executor(self, tmp_path):
        """Drawdown past threshold -> executor disabled."""
        # Seed a tall peak, then a trough.
        from tools.bet_executor import BetExecutor
        executor, db = await _mk_executor_with_bankroll(tmp_path, starting=8_000.0)
        try:
            # Insert a historic peak of 10000 yesterday.
            yesterday = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            await db.execute(
                "INSERT INTO bankroll (timestamp, balance, change, description) VALUES (?, 10000.0, 0, 'peak')",
                (yesterday,),
            )
            await db.execute(
                "INSERT INTO bankroll_peak (observed_at, balance, note) VALUES (?, 10000.0, 'peak')",
                (yesterday,),
            )
            # Stub hypotheses table so _pause doesn't KeyError.
            await db.execute("""
                CREATE TABLE hypotheses (
                    hypothesis_id TEXT PRIMARY KEY,
                    status TEXT,
                    updated_at TEXT,
                    promoted_at TEXT,
                    promoted_by TEXT
                )
            """)
            await db.commit()

            status = await executor.check_drawdown_and_kill()
            assert status["triggered"] is True, status
            assert status["drawdown_pct"] >= 0.15
            assert not executor.is_enabled
        finally:
            await db.close()


# =============================================================================
# Section 5 — Odds conversion edge cases
# =============================================================================

class TestOddsConversion:
    """Math verification matrix. Every assertion is a closed-form answer."""

    def test_plus_100_is_even_money(self):
        from tools.math_utils import american_to_decimal, american_to_implied
        assert abs(american_to_decimal(+100) - 2.00) < 1e-9
        assert abs(american_to_implied(+100) - 0.50) < 1e-9

    def test_minus_100_is_even_money(self):
        from tools.math_utils import american_to_decimal, american_to_implied
        assert abs(american_to_decimal(-100) - 2.00) < 1e-9
        assert abs(american_to_implied(-100) - 0.50) < 1e-9

    def test_minus_110_is_standard_juice(self):
        from tools.math_utils import american_to_decimal, american_to_implied
        assert abs(american_to_decimal(-110) - 1.90909090909) < 1e-8
        assert abs(american_to_implied(-110) - 0.52380952381) < 1e-8

    def test_plus_10000_extreme_longshot(self):
        """A +10000 longshot implies ~1% probability."""
        from tools.math_utils import american_to_implied, american_to_decimal
        assert abs(american_to_decimal(+10000) - 101.0) < 1e-9
        assert abs(american_to_implied(+10000) - 100 / 10100) < 1e-9

    def test_minus_10000_extreme_favorite(self):
        """A -10000 monster favorite implies ~99%."""
        from tools.math_utils import american_to_implied, american_to_decimal
        assert abs(american_to_decimal(-10000) - 1.01) < 1e-9
        assert abs(american_to_implied(-10000) - 10000 / 10100) < 1e-9

    def test_zero_odds_raises_everywhere(self):
        """0 is not a valid American odds value — every converter must reject it."""
        from tools.math_utils import american_to_decimal, american_to_implied
        from tools.odds_api import calculate_implied_probability
        from tools.kelly import _american_to_decimal as kelly_a2d
        with pytest.raises(ValueError):
            american_to_decimal(0)
        with pytest.raises(ValueError):
            american_to_implied(0)
        with pytest.raises(ValueError):
            calculate_implied_probability(0)
        with pytest.raises(ValueError):
            kelly_a2d(0)

    def test_decimal_lte_one_raises(self):
        from tools.math_utils import decimal_to_american
        with pytest.raises(ValueError):
            decimal_to_american(1.0)
        with pytest.raises(ValueError):
            decimal_to_american(0.5)

    def test_roundtrip_american_to_decimal_to_american(self):
        """Every round-trippable American odds value must survive conversion.

        Exception: +100 and -100 both mean even money (decimal 2.00). The
        canonical form decimal_to_american returns is +100, so -100 does
        not round-trip (it becomes +100). That's not a bug — it's a
        degenerate case of the American notation.
        """
        from tools.math_utils import american_to_decimal, decimal_to_american
        for odds in [-1000, -500, -200, -150, -110, +100, +150, +200, +500, +1000]:
            dec = american_to_decimal(odds)
            back = decimal_to_american(dec)
            assert back == odds, f"{odds} -> {dec} -> {back}"


# =============================================================================
# Section 6 — Devig math
# =============================================================================

class TestDevig:
    """Devig math sanity — symmetry on 2-way, non-negative on 3-way."""

    def test_two_way_symmetric_about_half(self):
        """Symmetric input -> symmetric output (both 50%)."""
        from tools.devig import devig_market
        result = devig_market([1.909, 1.909], method="multiplicative")
        p_a, p_b = result["fair_probabilities"]
        assert abs(p_a - 0.5) < 1e-6
        assert abs(p_b - 0.5) < 1e-6
        assert abs(p_a + p_b - 1.0) < 1e-9

    def test_two_way_power_method_sums_to_one(self):
        from tools.devig import devig_market
        result = devig_market([1.909, 1.909], method="power")
        total = sum(result["fair_probabilities"])
        assert abs(total - 1.0) < 1e-6

    def test_three_way_no_negative_probs(self):
        """3-way market (draw) — no method should return negative probs."""
        from tools.devig import devig_market
        # Soccer-style 3-way with meaningful overround.
        result = devig_market([2.10, 3.40, 3.60], method="auto")
        assert all(p > 0 for p in result["fair_probabilities"])
        assert abs(sum(result["fair_probabilities"]) - 1.0) < 1e-4

    def test_devig_american_flip_symmetric(self):
        """devig(-110, -110) should produce (0.5, 0.5)."""
        from tools.devig import devig_american
        r = devig_american(-110, -110, method="multiplicative")
        assert abs(r["side_a"]["fair_prob"] - 0.5) < 1e-6
        assert abs(r["side_b"]["fair_prob"] - 0.5) < 1e-6

    def test_ev_binary_zero_edge_at_fair_price(self):
        """EV = 0 when fair_prob equals implied (no vig, no edge)."""
        from tools.ev import ev_binary
        # Implied at +100 is 0.5, decimal is 2.0 -> EV = 0.5 * 2.0 - 1 = 0.
        assert abs(ev_binary(fair_prob=0.5, decimal_odds=2.0) - 0.0) < 1e-9

    def test_ev_binary_positive_edge(self):
        """p=0.55 at decimal=2.05 -> EV = 0.1275 (known)."""
        from tools.ev import ev_binary
        assert abs(ev_binary(0.55, 2.05) - 0.1275) < 1e-9


# =============================================================================
# Section 7 — Risk report endpoint helper
# =============================================================================

class TestRiskReport:
    @pytest.mark.asyncio
    async def test_risk_report_fields_present(self, tmp_path):
        """The report contains every cap + tripped-breaker field we promise."""
        from tools.risk_limits import compute_risk_report, RiskLimits

        db = await aiosqlite.connect(str(tmp_path / "r.db"))
        await db.execute("""CREATE TABLE bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            balance REAL NOT NULL, change REAL, bet_id INTEGER, description TEXT)""")
        await db.execute("""CREATE TABLE bets (
            bet_id INTEGER PRIMARY KEY AUTOINCREMENT, placed_at TEXT, sport TEXT,
            event_id TEXT, stake REAL, result TEXT, payout REAL)""")
        await db.execute(
            "INSERT INTO bankroll (timestamp, balance, change, description) VALUES (?, 10000.0, 0, 'seed')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()
        try:
            r = await compute_risk_report(db, limits=RiskLimits.from_env())
        finally:
            await db.close()

        for k in ("bankroll", "rolling_peak", "drawdown_pct", "open_exposure",
                  "daily_risk", "daily_pnl", "per_sport", "per_game",
                  "tripped_breakers", "limits", "circuit_breakers"):
            assert k in r, f"missing field {k}"
        assert r["bankroll"] == 10_000.0
        # With no bets, no breakers should fire.
        assert r["tripped_breakers"] == []

    @pytest.mark.asyncio
    async def test_risk_report_flags_open_exposure_breach(self, tmp_path):
        from tools.risk_limits import compute_risk_report, RiskLimits

        db = await aiosqlite.connect(str(tmp_path / "r.db"))
        await db.execute("""CREATE TABLE bankroll (
            id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp TEXT NOT NULL,
            balance REAL NOT NULL, change REAL, bet_id INTEGER, description TEXT)""")
        await db.execute("""CREATE TABLE bets (
            bet_id INTEGER PRIMARY KEY AUTOINCREMENT, placed_at TEXT, sport TEXT,
            event_id TEXT, stake REAL, result TEXT, payout REAL)""")
        await db.execute(
            "INSERT INTO bankroll (timestamp, balance, change, description) VALUES (?, 1000.0, 0, 'seed')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        # Cap at 25% of 1000 = 250. Seed 300 pending -> breached.
        await db.execute(
            "INSERT INTO bets (placed_at, sport, stake, result) VALUES (?, 'mlb', 300.0, 'pending')",
            (datetime.now(timezone.utc).isoformat(),),
        )
        await db.commit()
        try:
            r = await compute_risk_report(db, limits=RiskLimits.from_env())
        finally:
            await db.close()
        assert "max_open_exposure_pct" in r["tripped_breakers"]
        assert r["open_exposure"]["utilization"] > 1.0
