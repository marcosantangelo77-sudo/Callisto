"""Adversarial regression tests for the market-book sanity boundary.

Covers five independent-review blockers:
  1. tools/quant/consensus_engine.py must gate every source book and its
     placement through the shared market-sanity validator before devigging.
  2. tools/edge.clv_points returns None (not ValueError) for invalid
     source-side market data, while still raising on clean-audit corruption.
  3. tools/local_compute.local_devig honours its documented [-110, -110]
     American-odds contract and rejects invalid inputs safely.
  4. tools/boost_evaluator.devig_multibook never fabricates fairness from
     empty/malformed books or invalid odds.
  5. tools/devig explicit power/shin paths normalise valid tiny positive
     holds instead of echoing raw implied values.
"""

import asyncio
import math

import pytest

from tools.devig import power_devig, shin_devig
from tools.edge import MarketQuote, clv_points
from tools.local_compute import local_devig
from tools.quant.consensus_engine import (
    BookLine,
    compute_consensus_fair_prob,
)
from tools.quant.edge_ranker import MarketSnapshot, rank_edges


# ──────────────────────────────────────────────────────────────────────
# Issue 1 — consensus_engine gates every source book and placement
# ──────────────────────────────────────────────────────────────────────


class TestConsensusGate:
    """Invalid source books must never create a trusted fair value."""

    def _healthy(self):
        return [
            BookLine(book="pinnacle", implied_prob=0.5102, paired_implied_prob=0.5128),
            BookLine(book="fanduel", implied_prob=0.5200, paired_implied_prob=0.5000),
        ]

    def test_crossed_book_excluded(self):
        lines = [BookLine(book="pinnacle", implied_prob=0.60, paired_implied_prob=0.60)]
        # All-invalid input: no lines survive devigging.
        with pytest.raises(ValueError):
            compute_consensus_fair_prob(lines)
        # Mixed input: the crossed book is excluded, valid books still agree.
        result = compute_consensus_fair_prob(lines + self._healthy())
        assert math.isfinite(result.fair_prob)
        assert 0.0 < result.fair_prob < 1.0

    def test_nan_book_never_trusted(self):
        lines = self._healthy() + [
            BookLine(book="betmgm", implied_prob=float("nan"),
                     paired_implied_prob=0.50),
        ]
        result = compute_consensus_fair_prob(lines)
        assert "betmgm" not in result.per_book_fair
        assert math.isfinite(result.fair_prob)
        assert 0.0 < result.fair_prob < 1.0
        # A NaN-only consensus is impossible, not a number.
        with pytest.raises(ValueError):
            compute_consensus_fair_prob([
                BookLine(book="betmgm", implied_prob=float("nan"),
                         paired_implied_prob=0.50)])

    def test_high_hold_book_excluded(self):
        # ~30% hold exceeds the 20% market-sanity ceiling.
        lines = self._healthy() + [
            BookLine(book="hardrock", implied_prob=0.65, paired_implied_prob=0.65),
        ]
        result = compute_consensus_fair_prob(lines)
        assert "hardrock" not in result.per_book_fair

    def test_valid_low_hold_books_preserved_and_identities_kept(self):
        result = compute_consensus_fair_prob(self._healthy())
        assert set(result.per_book_fair) == {"pinnacle", "fanduel"}
        assert abs(sum(result.per_book_fair.values()) / 2 - 0.5) < 0.05

    def test_score_path_skips_invalid_placement(self):
        """A zero-hold placement book can never produce a recommendation."""
        snap = MarketSnapshot(
            sport="test",
            event_id="e1",
            market="h2h",
            outcome="A @ B",
            placement_line=BookLine(book="fanatics",
                                    implied_prob=0.60,
                                    paired_implied_prob=0.60),  # zero-hold
            all_lines=self._healthy() + [BookLine(book="fanatics",
                                                  implied_prob=0.60,
                                                  paired_implied_prob=0.60)],
        )
        ranked = rank_edges([snap])
        assert len(ranked) == 1
        assert ranked[0].decision == "skip"

    def test_scanner_drops_invalid_source_pair(self):
        from tools.quant.scanner import _snapshot_rows_from_games
        games = [{
            "id": "g1", "home_team": "B", "away_team": "A",
            "bookmakers": [
                {"key": "pinnacle", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "A", "price": -100}, {"name": "B", "price": 100}]}]},
                {"key": "fanduel", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "A", "price": -110}, {"name": "B", "price": 100}]}]},
                {"key": "betmgm", "markets": [{"key": "h2h", "outcomes": [
                    {"name": "A", "price": -108}, {"name": "B", "price": 102}]}]},
            ],
        }]
        placement_books = {"fanatics", "pinnacle", "fanduel"}
        # Pinnacle's +100/-100 pair is a zero-hold book → dropped; the valid
        # low-hold books survive.
        rows = _snapshot_rows_from_games(games, "test", placement_books)
        books = {r.placement_line.book for r in rows}
        assert books == {"fanduel"}

        # Control: a valid just-below-fair pair (-108/+104, ~2% hold)
        # passes the gate.
        games[0]["bookmakers"][0]["markets"][0]["outcomes"] = [
            {"name": "A", "price": -108}, {"name": "B", "price": 104}]
        rows = _snapshot_rows_from_games(games, "test", placement_books)
        books = {r.placement_line.book for r in rows}
        assert books == {"fanduel", "pinnacle"}


# ──────────────────────────────────────────────────────────────────────
# Issue 2 — clv_points None contract on invalid SOURCE-side data
# ──────────────────────────────────────────────────────────────────────


class TestClvPointsInvalidSource:
    def test_nan_claim_price_returns_none_not_error(self):
        claim = MarketQuote(float("nan"), .46, "probability")
        close = MarketQuote(.52, .49, "probability")
        assert clv_points(claim, close) is None

    def test_inf_close_price_returns_none(self):
        claim = MarketQuote(.48, .53, "probability")
        close = MarketQuote(float("inf"), float("inf"), "probability")
        assert clv_points(claim, close) is None

    def test_invalid_close_counter_returns_none(self):
        claim = MarketQuote(.48, .53, "probability")
        close = MarketQuote(.52, float("nan"), "probability")
        assert clv_points(claim, close) is None

    def test_clean_audit_corruption_still_raises(self):
        """Corruption of an otherwise clean audit must NOT be masked."""
        claim = MarketQuote(.48, .53, "probability")
        close = MarketQuote(.52, .49, "probability")
        f, a = close.fair_probability()
        assert a.get("devigged") is True
        real_fair_probability = MarketQuote.fair_probability

        def corrupted(self):
            return float("nan"), {"devigged": True, "method": "power"}

        try:
            MarketQuote.fair_probability = corrupted
            with pytest.raises(ValueError):
                clv_points(claim, close)
        finally:
            MarketQuote.fair_probability = real_fair_probability

    def test_healthy_control_still_scores(self):
        v = clv_points(MarketQuote(.46, .55, "probability"),
                       MarketQuote(.52, .49, "probability"))
        assert v is not None and math.isfinite(v)


# ──────────────────────────────────────────────────────────────────────
# Issue 3 — local_devig documented [-110, -110] call
# ──────────────────────────────────────────────────────────────────────


class TestLocalDevigContract:
    def test_documented_american_call_works(self):
        probs = asyncio.run(local_devig([-110, -110], method="power"))
        assert all(math.isfinite(p) for p in probs)
        assert abs(sum(probs) - 1.0) < 1e-6
        assert all(abs(p - 0.5) < 0.02 for p in probs)

    @pytest.mark.parametrize("method", ["power", "shin", "multiplicative"])
    def test_all_methods_accept_american_odds(self, method):
        probs = asyncio.run(local_devig([-110, 105], method=method))
        assert all(math.isfinite(p) for p in probs)
        assert abs(sum(probs) - 1.0) < 1e-6

    def test_implied_prob_input_accepted(self):
        probs = asyncio.run(local_devig([0.5238, 0.5238]))
        assert all(abs(p - 0.5) < 0.02 for p in probs)

    @pytest.mark.parametrize("bad", [[50, -110], [float("nan"), -110],
                                     [0, -110], [], [-110]])
    def test_invalid_inputs_rejected(self, bad):
        with pytest.raises((ValueError, ZeroDivisionError)):
            asyncio.run(local_devig(bad))

    def test_unknown_method_rejected(self):
        with pytest.raises(ValueError):
            asyncio.run(local_devig([-110, -110], method="bogus"))


# ──────────────────────────────────────────────────────────────────────
# Issue 4 — boost_evaluator never fabricates fairness
# ──────────────────────────────────────────────────────────────────────


class TestBoostMultibookNoFabrication:
    def test_empty_and_malformed_return_none(self):
        from tools.boost_evaluator import devig_multibook
        assert devig_multibook([]) is None
        assert devig_multibook([{}]) is None

    def test_invalid_fractional_odds_entry_dropped(self):
        from tools.boost_evaluator import devig_multibook
        books = [
            {"bookmaker": "pinnacle", "odds_for": -110, "odds_against": 100.9},
            {"bookmaker": "fanduel", "odds_for": -112, "odds_against": -108},
        ]
        # Only the valid book remains → its devigged fair value.
        result = devig_multibook(books)
        assert result is not None
        assert abs(result - 0.504330) < 1e-4

    def test_all_invalid_returns_none(self):
        from tools.boost_evaluator import devig_multibook
        # Zero-hold (+100/-100), invalid magnitude (50), missing fields.
        assert devig_multibook([
            {"bookmaker": "pinnacle", "odds_for": +100, "odds_against": -100},
            {"bookmaker": "fanduel", "odds_for": 50, "odds_against": -110},
            {"bookmaker": "draftkings"},
        ]) is None

    def test_invalid_boosted_odds_never_slam(self):
        """Fractional American odds like 100.9 can never reach a SLAM."""
        from tools.boost_evaluator import evaluate_fixed_boost
        with pytest.raises(ValueError):
            evaluate_fixed_boost(boosted_odds=100.9, fair_probability=0.55)
        with pytest.raises(ValueError):
            evaluate_fixed_boost(boosted_odds=-110, fair_probability=1.5)
        result = evaluate_fixed_boost(boosted_odds=-110, fair_probability=0.55)
        assert "SLAM" not in result["recommendation"]
        assert result["ev_pct"] < 15


# ──────────────────────────────────────────────────────────────────────
# Issue 5 — explicit power/shin paths normalise tiny positive holds
# ──────────────────────────────────────────────────────────────────────


class TestExplicitDevigTinyHoldNormalisation:
    JUST_BELOW_FAIR = [1 / .5000495, 1 / .5000495]

    @pytest.mark.parametrize("fn", [power_devig, shin_devig])
    def test_tiny_positive_hold_sums_to_one(self, fn):
        raw_sum = sum(1 / o for o in self.JUST_BELOW_FAIR)
        assert raw_sum > 1.0
        fair, param = fn(self.JUST_BELOW_FAIR)
        assert abs(sum(fair) - 1.0) < 1e-9
        assert abs(sum(fair) - raw_sum) > 1e-6  # normalised, not echoed

    def test_exactly_fair_still_rejected(self):
        with pytest.raises(ValueError):
            power_devig([2.0, 2.0])

    def test_asymmetric_tiny_hold_normalised(self):
        fair, k = power_devig([1 / 0.52, 1 / 0.4905])  # ~1.5% hold
        assert abs(sum(fair) - 1.0) < 1e-9
        assert k > 1.0
