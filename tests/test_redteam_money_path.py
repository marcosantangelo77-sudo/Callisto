"""RED TEAM — money path arithmetic (devig / EV / Kelly / CLV).

Surface: tools/devig.py, tools/ev.py, tools/kelly.py, tools/sizing.py,
tools/edge.py, tools/clv_tracker.py, tools/order_reconciler.py,
tools/cache_manager.py — none previously attacked.

Method: property sweeps over parameter spaces + differential between
independent implementations that MUST agree + one recorded-scenario replay.

FAILING tests document CONFIRMED breaks (each maps to a finding ID in
findings/redteam_money_path.md). PASSING tests at the bottom are
honest-negative pins: attacks that did NOT land, kept as regression pins.

Run: python3 -m pytest tests/test_redteam_money_path.py -q  (read-only;
no execution path is armed, nothing writes outside tmp dirs)
"""
import asyncio
import math
import os
import tempfile

import pytest

from tools.devig import devig_market, multiplicative_devig, power_devig, shin_devig
from tools.ev import ev_binary, ev_with_push, evaluate_edge
from tools.kelly import (
    AGP_TIER_MULTIPLIERS,
    _american_to_decimal,
    calculate_units,
    kelly_full,
    kelly_fractional,
    kelly_portfolio,
    ruin_probability,
    timing_value,
)
from tools.odds_api import calculate_implied_probability
from tools.sizing import best_price, kelly_binary

AMERICAN_POOL = [-400, -300, -250, -200, -180, -160, -150, -140, -130, -120,
                 -115, -110, -105, 100, 105, 110, 115, 120, 130, 150, 180,
                 200, 250, 300, 400, 500]


def _exact_kelly(edge: float, american: float) -> float:
    """Reference Kelly computed without rounding."""
    implied = calculate_implied_probability(int(american))
    p = max(0.0, min(1.0, implied + edge))
    dec = 1 + american / 100 if american > 0 else 1 + 100 / abs(american)
    b = dec - 1
    if b <= 0:
        return 0.0
    return max(0.0, (b * p - (1 - p)) / b)


# ===========================================================================
# M1 — kelly_full rounds UP: an automated actor raising a stake fraction
# ===========================================================================
def test_m1_kelly_full_never_rounds_up():
    violations = []
    for i in range(20000):
        edge = -0.10 + 0.30 * (i % 977) / 977
        odds = AMERICAN_POOL[i % len(AMERICAN_POOL)]
        f = kelly_full(edge, odds)
        exact = _exact_kelly(edge, odds)
        if f > exact + 1e-12:
            violations.append((edge, odds, f, exact))
    assert not violations, (
        f"kelly_full rounded UP in {len(violations)}/20000 cases "
        f"(first: {violations[0]}) — round() is a stake-inflation bonus path"
    )


def test_m1_kelly_fractional_inherits_round_up():
    # quarter-Kelly of the worst case still exceeds exact quarter-Kelly
    edge, odds = 0.0068, 110
    exact = _exact_kelly(edge, odds) * 0.25
    got = kelly_fractional(edge, odds)
    assert got <= exact + 1e-12, f"kelly_fractional {got} > exact {exact}"


# ===========================================================================
# M3 — portfolio Kelly treats perfectly-correlated duplicates as
#      diversification: rho=1.0 pair gets 2x the exposure of one bet
# ===========================================================================
def test_m3_duplicate_bets_at_rho_one_are_not_doubled():
    dup = [{"edge": 0.05, "odds": 110, "correlation_with_others": 1.0}] * 2
    res = kelly_portfolio(dup)
    total = res[0]["portfolio_summary"]["final_total_allocation"]
    single = kelly_portfolio(
        [{"edge": 0.05, "odds": 110, "correlation_with_others": 1.0}]
    )[0]["final_fraction"]
    assert total <= single * 1.01, (
        f"two rho=1.0 duplicates allocated {total:.4f} vs {single:.4f} for ONE "
        "bet — perfect correlation must collapse to a single position, not double"
    )


def test_m3_negative_correlation_not_treated_as_zero():
    def tot(rhos):
        bets = [{"edge": 0.04, "odds": -110, "correlation_with_others": r}
                for r in rhos]
        return kelly_portfolio(bets)[0]["portfolio_summary"][
            "final_total_allocation"]

    hedged = tot([-1.0, -1.0])
    indep = tot([0.0, 0.0])
    assert hedged != pytest.approx(indep), (
        "a fully-hedged book (rho=-1) receives exactly the same allocation as "
        "an uncorrelated one — negative correlation is clipped away"
    )


def test_m3_ten_duplicates_get_near_full_uncorrelated_exposure():
    bets = [{"edge": 0.04, "odds": -110, "correlation_with_others": 1.0}] * 10
    res = kelly_portfolio(bets)
    total = res[0]["portfolio_summary"]["final_total_allocation"]
    indep = kelly_portfolio(
        [{"edge": 0.04, "odds": -110, "correlation_with_others": 0.0}] * 10
    )[0]["portfolio_summary"]["final_total_allocation"]
    assert total < indep * 0.35, (
        f"ten copies of ONE bet ({total:.4f}) approach ten independent bets "
        f"({indep:.4f}) — the 1/sqrt(N) 'penalty' rewards stacking"
    )


# ===========================================================================
# M4 — ruin_probability: boundary crash + formula differential vs simulation
# ===========================================================================
def test_m4_zero_ev_boundary_returns_complete_result():
    # win_rate*b == q exactly -> ev_per_bet == -0.0 -> takes the neg-EV branch
    # which omits risk_level/recommended_max_stake_pct consumers expect.
    r = ruin_probability(2000, 25, 0.60, -150)
    for key in ("risk_level", "recommended_max_stake", "ruin_probability"):
        assert key in r, f"boundary case drops '{key}' — callers KeyError"


def test_m4_analytical_ruin_not_below_simulation_by_huge_margin():
    diffs = []
    for wr, odds, stake, bank in [(0.54, -110, 50, 1000),
                                  (0.52, 110, 20, 1000),
                                  (0.56, -120, 100, 5000)]:
        a = ruin_probability(bank, stake, wr, odds, "analytical")
        s = ruin_probability(bank, stake, wr, odds, "simulation")
        diffs.append((wr, odds, a["ruin_probability"], s["ruin_probability"]))
    for wr, odds, ana, sim in diffs:
        if sim > 0.001:
            assert ana >= sim * 0.75, (
                f"analytical ruin {ana} << simulated {sim} (wr={wr}, "
                f"odds={odds}) — closed form understates tail risk"
            )


# ===========================================================================
# M5 — calculate_units ignores price entirely
# ===========================================================================
def test_m5_unit_sizing_ignores_price_documented():
    # fraction = edge * kelly_fraction * tier_mult — no odds term anywhere.
    a = calculate_units(1000, 0.03, 0.8)
    b = calculate_units(1000, 0.03, 0.8)
    assert a["breakdown"].get("kelly_fraction") == 0.25
    assert a["dollar_amount"] == b["dollar_amount"]
    # DOCUMENTED GAP: identical stakes for a 3pt edge at -400 and at +400.


# ===========================================================================
# M6 — MarketQuote auto-detection misreads cent-quoted contracts
# ===========================================================================
def test_m6_kalshi_contract_50c_is_not_decimal_odds_50():
    from tools.edge import MarketQuote, assess_edge
    q = MarketQuote(price=50)          # a 50-cent prediction-market contract
    assert abs(q.implied_probability() - 0.50) < 0.01, (
        f"50-cent contract parsed as implied {q.implied_probability()} "
        "(decimal-odds interpretation) instead of 0.50"
    )
    ea = assess_edge("t", 0.90, q)
    assert not ea.actionable, (
        "misparse produced phantom edge 0.88 and actionable=True"
    )


def test_m6_contract_at_100_cents_is_not_even_money():
    from tools.edge import MarketQuote
    q = MarketQuote(price=100)         # $1.00 contract = certain payout
    assert q.implied_probability() > 0.99, (
        f"$1.00 contract parsed as even money ({q.implied_probability()})"
    )


# ===========================================================================
# M7 — evaluate_edge accepts impossible probabilities
# ===========================================================================
def test_m7_evaluate_edge_rejects_fair_prob_above_one():
    with pytest.raises(ValueError):
        evaluate_edge(1.5, -110)


def test_m7_evaluate_edge_rejects_negative_fair_prob():
    with pytest.raises(ValueError):
        evaluate_edge(-0.2, -110)


# ===========================================================================
# M8/F2 — three clv_log writers disagree on units and sign
# ===========================================================================
def _hv(implied, vig):
    return implied / (1 + vig / 2)


def _ai(o):
    return 100 / (o + 100) if o > 0 else abs(o) / (abs(o) + 100)


def test_f2_order_reconciler_raw_delta_disagrees_with_canonical_writer():
    # The reconciler computes clv_prob_bp = raw(closing)-raw(placement);
    # the canonical writer devigs both sides. Same bet, different numbers.
    placement, close = -105, -120     # clearly beat the close
    place_i, close_i = _ai(placement), _ai(close)
    tracker_bp = (_hv(close_i, 0.025) - _hv(place_i, 0.05)) * 10000   # canonical
    reconciler_bp = (close_i - place_i) * 10000                       # raw
    assert abs(tracker_bp - reconciler_bp) > 25, (
        "writers unexpectedly agree; premise of finding changed"
    ) if False else True
    # THE DEFECT: the reconciler ships its raw number into the SAME column
    # (clv_log.clv_prob_bp) the promotion gate reads as canonical devigged bp.
    # A bet that merely TIED the close on a high-vig book scores 0bp there
    # but +63bp canonically — and 412/26,244 odd pairs flip SIGN outright.
    flips = 0
    for place in list(range(-500, -95, 5)) + list(range(100, 505, 5)):
        pi, pf = _ai(place), _hv(_ai(place), 0.05)
        for close in list(range(-500, -95, 5)) + list(range(100, 505, 5)):
            ci, cf = _ai(close), _hv(_ai(close), 0.025)
            if ((ci - pi) > 0) != ((cf - pf) > 0):
                flips += 1
    assert flips > 300, "sign-flip census changed; re-derive finding"


def test_f2_order_reconciler_close_reliable_set_diverges():
    # clv_tracker trusts canonicalized {pinnacle, lowvig.ag, circa,
    # betfair_exchange}; the reconciler hand-rolls {"pinnacle","circa"}
    # against a raw .lower() string. Same field, different semantics.
    from tools.book_keys import canonicalize_book
    trusted_tracker = {"pinnacle", "lowvig.ag", "circa", "betfair_exchange"}
    trusted_reconciler = {"pinnacle", "circa"}
    probe = ["lowvig.ag", "LowVig.ag ", "betfair exchange"]
    divergent = [
        b for b in probe
        if (canonicalize_book(b) in trusted_tracker)
        != (b.strip().lower() in trusted_reconciler)
    ]
    assert divergent, "reliability sets converged; finding stale"


# ===========================================================================
# F10 — record_closing_line has NO point matching: one close stamps every
#        spread bet on the team regardless of line
# ==========================================================================
def _run_f10_scenario():
    async def main():
        from tools.clv_tracker import CLVTracker
        db = tempfile.mktemp(suffix=".db")
        t = CLVTracker(db)
        await t.initialize()
        await t._db.execute("""CREATE TABLE IF NOT EXISTS paper_trades (
            trade_id TEXT PRIMARY KEY, event_id TEXT, market TEXT, side TEXT,
            signal_odds_american INTEGER, signal_implied_prob REAL,
            closing_odds INTEGER, closing_implied REAL,
            closing_implied_prob REAL, clv_implied REAL, line REAL,
            book TEXT, sport TEXT, actual_result TEXT,
            hypothetical_pnl REAL)""")
        await t._db.execute("""CREATE TABLE IF NOT EXISTS clv_log (
            bet_id TEXT PRIMARY KEY, event TEXT, outcome TEXT, point REAL,
            book TEXT, our_odds_decimal REAL, pinnacle_close_fair_prob REAL,
            pinnacle_close_fair_decimal REAL, clv_cents REAL,
            clv_prob_bp REAL, actual_result TEXT, actual_pnl REAL,
            close_reliable INTEGER, logged_at TEXT,
            regime_phase_at_placement TEXT)""")
        await t._db.commit()
        b1 = await t.record_bet("basketball_nba", "G1", "Lakers", "spreads",
                                "fanduel", -110, placement_point=3.5,
                                event_id="E1")
        b2 = await t.record_bet("basketball_nba", "G1", "Lakers", "spreads",
                                "fanduel", -110, placement_point=-2.0,
                                event_id="E1")
        # one closing line, at NEITHER bet's number
        await t.record_closing_line("E1", "spreads", "Lakers", -105,
                                    closing_point=7.5, source="pinnacle",
                                    sport="basketball_nba")
        cur = await t._db.execute(
            "SELECT id, placement_point, closing_point FROM bets ORDER BY id")
        rows = [tuple(r) for r in await cur.fetchall()]
        await t.resolve_bet(b1, "won", 95)
        await t.resolve_bet(b2, "lost", None)
        cur = await t._db.execute(
            "SELECT bet_id, point, clv_prob_bp FROM clv_log ORDER BY bet_id")
        logs = [tuple(r) for r in await cur.fetchall()]
        await t.close()
        os.unlink(db)
        return rows, logs

    return asyncio.run(main())


def test_f10_closing_line_does_not_leak_across_points():
    rows, logs = _run_f10_scenario()
    by_id = {r[0]: r for r in rows}
    # Bet 1 was placed at +3.5; the recorded close is 7.5.
    assert by_id[1][2] != 7.5, (
        "closing line for point 7.5 stamped onto the +3.5 bet"
    )
    # Bet 2 was placed at -2.0.
    assert by_id[2][2] != 7.5, (
        "the SAME closing line also stamped onto the -2.0 bet"
    )


def test_f10_both_points_get_identical_clv_bp_today():
    rows, logs = _run_f10_scenario()
    bp = {r[0]: r[2] for r in logs}
    assert bp["1"] == pytest.approx(bp["2"]), "premise drifted"
    # ...which is the defect: two very different true CLVs cannot share a value.


# ===========================================================================
# F11 — dashboard/context layers read the LEGACY raw column and label it
# ===========================================================================
def test_f11_context_layer_reads_legacy_column():
    import inspect
    import tools.cache_manager as cm
    src = inspect.getsource(cm.query_clv_summary)
    assert "clv_prob_bp" in src, (
        "query_clv_summary still aggregates bets.clv_implied (raw implied "
        "delta) and labels it avg_clv_cents — mixed units feed agent context"
    )


# ===========================================================================
# HONEST NEGATIVES — attacks that did NOT land (regression pins)
# ===========================================================================
def test_neg_shin_solver_sums_to_one_on_random_markets():
    from random import Random
    rng = Random(7)
    for _ in range(300):
        odds = [rng.uniform(1.05, 20) for _ in range(3)]
        try:
            fair, _z = shin_devig(odds)
        except Exception:
            continue
        assert abs(sum(fair) - 1.0) < 1e-3


def test_neg_timing_value_never_waits_into_negative_ev():
    from random import Random
    rng = Random(5)
    for _ in range(1500):
        edge = rng.uniform(0.001, 0.10)
        h = rng.uniform(0.01, 72)
        sport = rng.choice(["basketball_nba", "americanfootball_nfl",
                            "icehockey_nhl"])
        market = rng.choice(["h2h", "totals", "player_threes"])
        r = timing_value(edge, h, sport, market)
        if r["recommendation"] == "WAIT":
            assert r["wait_ev"] > 0


def test_neg_sizing_kelly_binary_matches_kelly_full():
    from random import Random
    rng = Random(3)
    for _ in range(200):
        p = rng.uniform(0.05, 0.95)
        am = rng.choice([-200, -150, -110, 110, 150, 200])
        dec = _american_to_decimal(am)
        edge = p - calculate_implied_probability(am)
        assert abs(kelly_binary(p, dec) - kelly_full(edge, am)) < 1e-6


def test_neg_push_math_correct_on_docstring_example():
    # p_win=.52 p_push=.05 odds 1.909 -> EV 4.27%
    assert ev_with_push(0.52, 0.05, 1.909) == pytest.approx(0.0427, abs=1e-3)


def test_neg_best_price_picks_higher_decimal():
    r = best_price(-105, -110)
    assert r["best_book"] == "draftkings" and r["best_odds_american"] == -105


def test_neg_devig_balanced_book_returns_half():
    d = devig_market([2.0, 2.0])
    assert d["fair_probabilities"] == [0.5, 0.5]


def test_neg_multiplicative_preserves_ordering():
    from random import Random
    rng = Random(11)
    for _ in range(200):
        odds = sorted([rng.uniform(1.05, 20) for _ in range(3)])
        fair = multiplicative_devig(odds)   # odds ascending -> probs descending
        assert fair == sorted(fair, reverse=True)


def test_neg_power_devig_flb_direction_on_heavy_favorite():
    # heavy favorite: power fair prob >= multiplicative share
    oA, oB = 1.20, 6.0          # balanced-ish retail pair
    m = multiplicative_devig([oA, oB])
    p, _k = power_devig([oA, oB])
    assert p[0] >= m[0] - 1e-9
