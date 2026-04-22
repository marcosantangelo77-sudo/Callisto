"""Unit tests for the CLV unit-mix fix.

The audit found clv_tracker.py was mixing American-points (line 414) and
prob-basis-points (line 419) in the same clv_cents column. Going forward
the canonical unit is prob-basis-points. These tests verify:
  * clv_prob_bp has the correct magnitude and sign.
  * +150 → +130 (we got the underdog, close moved toward the favorite
    after us) produces POSITIVE CLV in prob-bp terms.
  * -110 → -130 (we got a worse number than the close) produces
    NEGATIVE CLV.
"""

import asyncio
import os
import tempfile

import pytest

from tools.odds_api import calculate_implied_probability
from tools.clv_tracker import CLVTracker, _half_vig_devig


def _implied(odds: int) -> float:
    return calculate_implied_probability(odds)


def test_clv_prob_bp_positive_when_placement_better_than_close():
    """We bet +150 and close moved to +130 — the market got LESS generous
    to our side after us, so we beat the close → positive CLV."""
    placement_imp = _implied(+150)   # ~0.400
    close_imp = _implied(+130)       # ~0.435
    placement_fair = _half_vig_devig(placement_imp, 0.05)
    close_fair = _half_vig_devig(close_imp, 0.025)
    clv_prob_bp = round((close_fair - placement_fair) * 10000, 1)
    assert clv_prob_bp > 0
    # Sanity: gap is 3-4 prob percentage points → ~300-400 bp range.
    assert 150 < clv_prob_bp < 500, f"Unexpected magnitude: {clv_prob_bp}"


def test_clv_prob_bp_negative_when_placement_worse_than_close():
    """We bet -130 and close moved to -110 — our book was worse than the
    close, so CLV is negative. This is the classic 'chased the wrong side'
    signature every CLV dashboard looks for."""
    placement_imp = _implied(-130)   # ~0.565
    close_imp = _implied(-110)       # ~0.524
    placement_fair = _half_vig_devig(placement_imp, 0.05)
    close_fair = _half_vig_devig(close_imp, 0.025)
    clv_prob_bp = round((close_fair - placement_fair) * 10000, 1)
    assert clv_prob_bp < 0


def test_clv_prob_bp_near_zero_on_tied_lines():
    """Same implied prob at placement and close → CLV ~ 0, sign is
    driven entirely by the vig-adjustment difference between placement_vig
    and closing_vig. Must stay within a small band (< 300 bp)."""
    imp = _implied(-110)
    placement_fair = _half_vig_devig(imp, 0.05)
    close_fair = _half_vig_devig(imp, 0.025)
    clv_prob_bp = round((close_fair - placement_fair) * 10000, 1)
    assert abs(clv_prob_bp) < 300


@pytest.mark.asyncio
async def test_log_clv_writes_prob_bp_column():
    """End-to-end: record a bet, record a closing line, resolve, and
    confirm the clv_log row has clv_prob_bp populated with the expected
    magnitude and sign.
    """
    import aiosqlite

    with tempfile.TemporaryDirectory() as tmp:
        db_path = os.path.join(tmp, "test.db")

        # Pre-create clv_log with both columns so this test doesn't depend
        # on the full schema migration running.
        async with aiosqlite.connect(db_path) as db:
            await db.execute("""
                CREATE TABLE clv_log (
                    bet_id TEXT PRIMARY KEY,
                    event TEXT, outcome TEXT, point REAL, book TEXT,
                    our_odds_decimal REAL,
                    pinnacle_close_fair_prob REAL,
                    pinnacle_close_fair_decimal REAL,
                    clv_cents REAL,
                    clv_prob_bp REAL,
                    actual_result TEXT, actual_pnl REAL,
                    close_reliable INTEGER, logged_at TEXT
                )
            """)
            # paper_trades — not used here but the tracker's initialize()
            # touches it; the tracker is intentionally constructed without
            # calling initialize() so we can focus on _log_clv.
            await db.commit()

        tracker = CLVTracker(db_path=db_path)
        tracker._db = await aiosqlite.connect(db_path)
        try:
            # Bet: +150, close: +130, result: won, payout: 250 on 100 stake.
            bet = {
                "id": 1, "event_id": "evt1", "team": "Team A",
                "placement_point": None, "bookmaker": "DraftKings",
                "closing_source": "Pinnacle",
                "placement_odds": +150, "closing_odds": +130,
                "placement_implied_prob": _implied(+150),
                "closing_implied_prob": _implied(+130),
            }
            await tracker._log_clv(bet, result="won", payout=250, change=150)
            await tracker._db.commit()

            cursor = await tracker._db.execute(
                "SELECT clv_prob_bp, close_reliable, book FROM clv_log WHERE bet_id = '1'"
            )
            row = await cursor.fetchone()
            assert row is not None
            clv_prob_bp, close_reliable, book = row
            assert clv_prob_bp is not None
            assert clv_prob_bp > 0, f"Expected positive CLV, got {clv_prob_bp}"
            # close_reliable must be True — 'Pinnacle' resolves via
            # canonicalize_book into the reliable allowlist.
            assert close_reliable == 1, (
                "closing_source='Pinnacle' must resolve to a reliable close"
            )
            # Book column must be stored canonicalized.
            assert book == "draftkings"
        finally:
            await tracker._db.close()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
