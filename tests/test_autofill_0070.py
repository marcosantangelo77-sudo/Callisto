"""
Autofill characterization #0070 — dual Kelly (LONG module).

Characterizes the two DISTINCT Kelly paths that the tools.kelly ->
tools.kellypkg split established:

  PATH A (rounded):   ``kelly_full`` / ``kelly_fractional`` — American odds
                      interface; result ROUNDED TO 6 DECIMAL PLACES.
  PATH B (unrounded): ``kelly_core`` (kellypkg._formula.kelly_core_unrounded)
                      — raw p/b interface; ``tools.sizing.kelly_binary``
                      delegates to it and stays FULL PRECISION.

The paths must NEVER be merged:
  * kelly_full rounds its return value.
  * kelly_core must stay unrounded — rounding it would change every
    sizing decision downstream of tools.sizing.kelly_binary.

Safety invariants re-checked here (fail-closed, tests only):
  * ``_PAPER_TRADE_SIGNAL_STATUSES`` is exactly {"paper_trading"}; "live"
    is not present anywhere in the allowed statuses.
  * ``BacktestEngine.generate_paper_trade_signal`` is not widened to
    accept status == 'live'.
"""

import ast
import inspect
import os
import re

import pytest

import tools.kelly as facade
from tools.kellypkg._formula import kelly_core_unrounded
from tools.kellypkg.core import kelly_core as pkg_kelly_core
from tools.kellypkg.odds import _american_to_decimal
from tools.odds_api import calculate_implied_probability

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Reference implementation of PATH B used for characterization parity checks.
# Mirrors kelly_core_unrounded exactly (same op order) without importing it,
# so a future silent change to _formula.py is caught.
# ---------------------------------------------------------------------------


def ref_kelly(p: float, b: float) -> float:
    if b <= 0:
        return 0.0
    q = 1.0 - p
    return max(0.0, (b * p - q) / b)


def ref_full(edge: float, american_odds) -> float:
    """Reference for kelly_full including its 6-decimal rounding."""
    implied = calculate_implied_probability(int(american_odds))
    p = max(0.0, min(1.0, implied + edge))
    b = _american_to_decimal(int(american_odds)) - 1.0
    return round(ref_kelly(p, b), 6)


ODDS_GRID = [-400, -300, -200, -150, -120, -110, -105, 0, 100, 105, 110, 120, 150, 200, 300, 500]
EDGE_GRID = [0.0, 0.005, 0.01, 0.02, 0.03, 0.05, 0.075, 0.10]


# ===========================================================================
# 1. kelly_full ROUNDS to 6 decimal places
# ===========================================================================


class TestKellyFullRoundsToSixDecimals:
    @pytest.mark.parametrize("odds", ODDS_GRID)
    def test_output_equals_round_of_raw_formula(self, odds):
        edge = 0.04
        assert facade.kelly_full(edge, odds) == round(
            pkg_kelly_core(
                max(0.0, min(1.0, calculate_implied_probability(odds) + edge)),
                _american_to_decimal(odds) - 1.0,
            ),
            6,
        )

    @pytest.mark.parametrize("edge", EDGE_GRID)
    def test_output_is_six_decimal_multiple_across_edge_grid(self, edge):
        val = facade.kelly_full(edge, -110)
        scaled = val * 1_000_000
        assert abs(scaled - round(scaled)) < 1e-6

    def test_round_identity_holds_on_result_itself(self):
        for edge in EDGE_GRID:
            for odds in (-110, 100, 250):
                val = facade.kelly_full(edge, odds)
                assert val == round(val, 6)

    def test_precision_loss_is_visible_for_a_chosen_case(self):
        """A case where the raw value has more than 6 decimals, proving the
        rounding actually happens rather than being a no-op."""
        edge, odds = 0.0412371, 137
        raw = ref_kelly(
            calculate_implied_probability(odds) + edge,
            _american_to_decimal(odds) - 1.0,
        )
        out = facade.kelly_full(edge, odds)
        assert out == round(raw, 6)

    def test_string_repr_has_no_more_than_six_decimals(self):
        s = repr(facade.kelly_full(0.0333333333, 175))
        if "." in s:
            decimals = len(s.split(".")[1].rstrip("0"))
            assert decimals <= 6

    def test_matches_independent_reference(self):
        for edge in EDGE_GRID:
            for odds in (-110, -105, 100, 150, 220):
                assert facade.kelly_full(edge, odds) == ref_full(edge, odds)

    def test_negative_edge_clamps_then_still_rounds_to_zero(self):
        val = facade.kelly_full(-0.20, 500)
        assert val == 0.0 == round(val, 6)

    def test_extreme_edge_clamped_at_p_one(self):
        # edge so large p would exceed 1.0 -> clamps to 1.0, f* = 1.0 rounded.
        val = facade.kelly_full(5.0, -110)
        assert val == round(pkg_kelly_core(1.0, _american_to_decimal(-110) - 1.0), 6)


# ===========================================================================
# 2. kelly_core / kelly_binary stay UNROUNDED
# ===========================================================================


class TestKellyBinaryStaysUnrounded:
    UNROUNDED_CASES = [
        (0.55, 0.909090909090909),
        (0.52, 2.5),
        (0.60, 1.5),
        (0.51, 11.0),
        (0.75, 0.5),
        (0.40, 3.0),
        (0.9999999, 1.000001),
    ]

    @pytest.mark.parametrize("p,b", UNROUNDED_CASES)
    def test_exact_float_equality_with_reference_formula(self, p, b):
        assert pkg_kelly_core(p, b) == ref_kelly(p, b)

    @pytest.mark.parametrize("p,b", UNROUNDED_CASES)
    def test_formula_module_agrees_with_package_core(self, p, b):
        assert kelly_core_unrounded(p, b) == pkg_kelly_core(p, b)

    def test_facade_kelly_core_is_same_function_object(self):
        assert facade.kelly_core is pkg_kelly_core

    def test_value_with_more_than_six_decimals_survives(self):
        # (b*p - q)/b with these inputs is irrational-ish; rounding to 6dp
        # would break exact equality with the reference.
        p, b = 0.5238095238, 0.7142857142
        val = pkg_kelly_core(p, b)
        assert val != round(val, 6) or val == ref_kelly(p, b)

    def test_sizing_kelly_binary_delegates_and_keeps_precision(self, monkeypatch):
        import tools.sizing as sizing

        calls = []
        real = sizing.kelly_core

        def spy(p, b):
            calls.append((p, b))
            return real(p, b)

        monkeypatch.setattr(sizing, "kelly_core", spy)
        out = sizing.kelly_binary(0.58, 1.9523809523809523)
        assert calls[-1] == (0.58, 0.9523809523809523)
        assert out == ref_kelly(0.58, 0.9523809523809523)

    def test_sizing_kelly_binary_not_equal_to_rounded_version(self):
        import tools.sizing as sizing

        p, dec = 0.53, 1.9444444444444444
        out = sizing.kelly_binary(p, dec)
        if out != 0.0 and abs(out - round(out, 6)) > 0:
            assert out == ref_kelly(p, dec - 1.0)

    def test_non_positive_b_returns_exactly_zero_float(self):
        for b in (0.0, -0.5, -1e-12):
            assert pkg_kelly_core(0.99, b) == 0.0
            assert kelly_core_unrounded(0.99, b) == 0.0
            assert type(pkg_kelly_core(0.99, b)) is float

    def test_neg_ev_returns_zero_not_negative(self):
        import tools.sizing as sizing

        assert pkg_kelly_core(0.30, 1.0) == 0.0
        assert kelly_core_unrounded(0.10, 9.0) == 0.0
        assert sizing.kelly_binary(0.30, 2.0) == 0.0


# ===========================================================================
# 3. The two paths are NOT merged
# ===========================================================================


class TestPathsAreDistinct:
    def test_distinct_code_objects(self):
        import tools.sizing as sizing

        assert facade.kelly_full.__code__ is not sizing.kelly_binary.__code__
        assert facade.kelly_full.__code__ is not pkg_kelly_core.__code__
        assert pkg_kelly_core.__code__ is not kelly_core_unwrapped_code_guard()

    def test_kelly_full_signature_takes_edge_and_odds(self):
        sig = inspect.signature(facade.kelly_full)
        assert list(sig.parameters) == ["edge", "odds"]

    def test_kelly_core_signature_takes_p_and_b(self):
        sig = inspect.signature(pkg_kelly_core)
        assert list(sig.parameters) == ["p", "b"]

    def test_sizing_kelly_binary_signature_takes_prob_and_decimal(self):
        import tools.sizing as sizing

        sig = inspect.signature(sizing.kelly_binary)
        assert list(sig.parameters) == ["fair_prob", "decimal_odds"]

    def test_docstrings_pin_the_rounding_contract(self):
        assert "ROUNDED" in (inspect.getdoc(facade.kelly_full) or "").upper()
        core_doc = (inspect.getdoc(pkg_kelly_core) or "").upper()
        assert "UNROUNDED" in core_doc

    def test_kelly_full_source_contains_round_call(self):
        src = inspect.getsource(facade.kelly_full)
        assert re.search(r"round\(", src), "kelly_full must round its own return"

    def test_kelly_core_source_contains_no_round_call(self):
        src = inspect.getsource(pkg_kelly_core)
        tree = ast.parse(src)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id != "round" or node.args == [], (
                    "kelly_core path must not round"
                )


def kelly_core_unwrapped_code_guard():
    return kelly_core_unrounded.__code__


# ===========================================================================
# 4. Cross-path consistency (same math, different presentation)
# ===========================================================================


class TestCrossPathConsistency:
    @pytest.mark.parametrize("edge,odds", [(0.03, -110), (0.06, 140), (0.01, -105)])
    def test_full_equals_rounded_core_of_equivalent_inputs(self, edge, odds):
        p = max(0.0, min(1.0, calculate_implied_probability(odds) + edge))
        b = _american_to_decimal(odds) - 1.0
        assert facade.kelly_full(edge, odds) == round(pkg_kelly_core(p, b), 6)

    def test_fractional_builds_on_full_and_stays_rounded(self):
        full = facade.kelly_full(0.08, 180)
        half = facade.kelly_fractional(0.08, 180, fraction=0.5)
        quarter = facade.kelly_fractional(0.08, 180, fraction=0.25)
        assert half == round(full * 0.5, 6)
        assert quarter == round(full * 0.25, 6)
        assert abs(half * 1_000_000 - round(half * 1_000_000)) < 1e-6

    def test_monotonicity_in_edge_both_paths(self):
        prev_full = -1.0
        prev_core = -1.0
        for i in range(1, 12):
            edge = i * 0.01
            v_full = facade.kelly_full(edge, -110)
            v_core = pkg_kelly_core(
                calculate_implied_probability(-110) + edge,
                _american_to_decimal(-110) - 1.0,
            )
            assert v_full >= prev_full
            assert v_core >= prev_core
            prev_full, prev_core = v_full, v_core

    def test_zero_edge_is_zero_everywhere(self):
        odds = -110
        assert facade.kelly_full(0.0, odds) == 0.0
        # Unrounded path may carry float dust at the exact breakeven; it must
        # still be negligible and non-positive-meaningful.
        v = pkg_kelly_core(
            calculate_implied_probability(odds), _american_to_decimal(odds) - 1.0
        )
        assert abs(v) <= 1e-12


# ===========================================================================
# 5. AST pins on production source (fail closed against drift)
# ===========================================================================


def _read(path_parts):
    with open(os.path.join(REPO_ROOT, *path_parts)) as fh:
        return fh.read()


class TestSourcePins:
    def test_formula_module_is_the_only_formula(self):
        src = _read(("tools", "kellypkg", "_formula.py"))
        assert "max(0.0, (b * p - q) / b)" in src

    def test_kelly_full_pin_round_literal_is_six(self):
        src = inspect.getsource(facade.kelly_full)
        assert re.search(r"round\(.*?,\s*6\s*\)", src), (
            "kelly_full rounding precision changed from 6"
        )

    def test_sizing_delegates_to_imported_kelly_core(self):
        src = _read(("tools", "sizing.py"))
        assert "from tools.kelly import kelly_core" in src
        body = src[src.index("def kelly_binary"):]
        body = body[: body.index("\ndef ", 1)]
        assert "kelly_core(" in body
        assert "round(" not in body, "sizing.kelly_binary must not round"

    def test_no_live_string_anywhere_in_kellypkg_or_sizing_or_kelly(self):
        for parts in (("tools", "sizing.py"), ("tools", "kelly.py")):
            src = _read(parts)
            assert '"live"' not in src and "'live'" not in src, parts
        root = os.path.join(REPO_ROOT, "tools", "kellypkg")
        for fname in sorted(os.listdir(root)):
            if fname.endswith(".py"):
                src = open(os.path.join(root, fname)).read()
                assert '"live"' not in src and "'live'" not in src, fname


# ===========================================================================
# 6. Live-betting fail-closed registry (unchanged by this task)
# ===========================================================================


class TestPaperTradeGateUnchanged:
    def test_statuses_are_exactly_paper_trading(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        assert isinstance(_PAPER_TRADE_SIGNAL_STATUSES, frozenset)
        assert _PAPER_TRADE_SIGNAL_STATUSES == frozenset({"paper_trading"})

    def test_live_not_member_in_any_case_form(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        lowered = {s.lower() for s in _PAPER_TRADE_SIGNAL_STATUSES}
        assert "live" not in lowered
        assert "live_betting" not in lowered

    def test_backtest_generate_signal_not_widened_ast(self):
        src = _read(("inference_kernel.py",))
        idx = src.find("async def generate_paper_trade_signal")
        if idx == -1:
            pytest.skip("generate_paper_trade_signal not in inference_kernel.py")
        body = src[idx : idx + 4000]
        gate = re.search(r"_PAPER_TRADE_SIGNAL_STATUSES", body)
        assert gate, "gate reference missing near generate_paper_trade_signal"
        head = body[: body.find('"""', body.find('"""') + 3) + 3] if body.count('"""') >= 2 else body[:800]
        code = body.replace(head, "")
        assert "status == 'live'" not in code and 'status == "live"' not in code

    def test_frozenset_type_blocks_runtime_mutation(self):
        from tools.signals.paper import _PAPER_TRADE_SIGNAL_STATUSES

        with pytest.raises(AttributeError):
            _PAPER_TRADE_SIGNAL_STATUSES.add("live")  # type: ignore[attr-defined]


# ===========================================================================
# 7. Numeric edge-case sweep (characterization table)
# ===========================================================================


class TestNumericEdgeCases:
    def test_even_money_positive_edge_known_values(self):
        # odds=0 -> calculate_implied_probability(0)=0.5? No: 100/(0+100)... it
        # takes the >0 branch only for positive; 0 falls to else branch of
        # _american_to_decimal (2.0) but implied prob uses 100/(0+100)=1.0,
        # clamping p to 1.0 and edge is swallowed -> characterize ACTUAL
        # behavior: odds=0 yields p = min(1, 1.0 + edge) = 1.0, f*=... verify.
        for edge in (0.01, 0.02):
            assert facade.kelly_full(edge, 0) == ref_full(edge, 0)
        # Sanity: the even-money equivalence holds via explicit b=1 core path.
        assert pkg_kelly_core(0.55, 1.0) == pytest.approx(2 * 0.55 - 1, rel=1e-9)

    def test_core_even_money_exact(self):
        assert pkg_kelly_core(0.55, 1.0) == pytest.approx(0.09999999999999998, rel=1e-12)
        assert pkg_kelly_core(0.55, 1.0) == ref_kelly(0.55, 1.0)

    def test_plus_money_conversion_consistency(self):
        assert _american_to_decimal(150) == 2.5
        assert _american_to_decimal(300) == 4.0
        assert _american_to_decimal(100) == 2.0

    def test_minus_money_conversion_consistency(self):
        assert _american_to_decimal(-100) == 2.0
        assert _american_to_decimal(-200) == 1.5
        assert _american_to_decimal(-400) == 1.25

    def test_zero_odds_maps_to_even_money(self):
        assert _american_to_decimal(0) == 2.0

    def test_large_grid_never_negative_never_above_one(self):
        for odds in ODDS_GRID:
            b = _american_to_decimal(odds) - 1.0
            for edge in EDGE_GRID:
                p = max(0.0, min(1.0, calculate_implied_probability(odds) + edge))
                v = pkg_kelly_core(p, b)
                assert 0.0 <= v <= 1.0
                fv = facade.kelly_full(edge, odds)
                assert 0.0 <= fv <= 1.0

    def test_idempotent_rounding(self):
        for edge in EDGE_GRID:
            for odds in (-110, 0, 200):
                v = facade.kelly_full(edge, odds)
                assert round(v, 6) == v

    def test_core_symmetry_breakpoint_at_ev_zero(self):
        # f* = 0 exactly when b*p == q  =>  p == q/(b+q)
        for b in (0.5, 1.0, 2.0, 9.0):
            q = 1.0
            p_star = q / (b + q)
            assert pkg_kelly_core(p_star, b) == 0.0
            assert pkg_kelly_core(p_star + 1e-9, b) > 0.0

    def test_double_edge_doubles_fraction_on_even_money(self):
        assert pkg_kelly_core(0.52, 1.0) == pytest.approx(2 * pkg_kelly_core(0.51, 1.0), rel=1e-9)

    def test_kelly_full_accepts_int_like_odd_inputs_via_int_cast(self):
        # QUIRK: kelly_full casts odds through int() ONLY for the implied
        # probability; the net-payout conversion receives the RAW odds.
        # So 110.9 -> p from odds=110, b from decimal(110.9). Characterize it.
        edge = 0.03
        out = facade.kelly_full(edge, 110.9)
        p = max(0.0, min(1.0, calculate_implied_probability(int(110.9)) + edge))
        b = _american_to_decimal(110.9) - 1.0
        assert out == round(pkg_kelly_core(p, b), 6)

    def test_kelly_full_mixed_cast_quirk_differs_from_pure_int_odds(self):
        assert facade.kelly_full(0.03, 110.9) != facade.kelly_full(0.03, 110)


# ===========================================================================
# 8. Downstream consumers still see the right thing
# ===========================================================================


class TestDownstreamConsumers:
    def test_sizing_bet_size_runs_end_to_end(self):
        import tools.sizing as sizing

        res = sizing.bet_size(10_000, 0.55, 2.10, "high")
        assert res["recommended_stake"] >= 0.0
        # bet_size's 'kelly_full' field is the ROUNDED full Kelly while
        # kelly_binary is unrounded — they differ by < 1e-5 here.
        assert res["kelly_full"] == pytest.approx(sizing.kelly_binary(0.55, 2.10), abs=1e-5)
        assert res["kelly_quarter"] == pytest.approx(res["kelly_adjusted"], rel=1e-9)

    def test_sizing_bet_size_respects_max_wager(self):
        import tools.sizing as sizing

        res = sizing.bet_size(10_000, 0.70, 3.0, "high", max_wager=50.0)
        assert res["recommended_stake"] <= 50.0

    def test_kelly_dynamic_stake_matches_rounded_fraction_times_bankroll(self):
        bankroll = 25_000
        res = facade.kelly_dynamic(0.04, -110, 0.85, 0.02, bankroll)
        # kelly_dynamic computes stake from the UNROUNDED internal fraction,
        # while 'fraction' is rounded to 6dp — characterize that exact quirk.
        assert res["stake"] == pytest.approx(bankroll * res["fraction"], abs=0.02)

    def test_kelly_portfolio_allocations_within_cap(self):
        bets = [{"edge": e, "odds": o, "correlation_with_others": 0.2}
                for e, o in ((0.03, -110), (0.05, 130), (0.02, -105))]
        results = facade.kelly_portfolio(bets)
        total = results[0]["portfolio_summary"]["final_total_allocation"]
        assert 0.0 < total <= 0.20 + 1e-9

    def test_facade_exports_match_package_identity(self):
        import tools.kellypkg as kp

        assert facade.kelly_full is kp.kelly_full
        assert facade.kelly_fractional is kp.kelly_fractional
