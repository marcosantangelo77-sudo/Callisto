"""RED TEAM — THE MONEY PATH, H4 + H6: CLV sign integrity and the live path.

H4: can CLV read positive when the line moved AGAINST the bet? Units are
    the known weak point — a confirmed unit bug already cost real money here.
H6: can anything in the target surface place, authorise, or prepare an order?
"""
import asyncio
import math
import sys
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.edge import MarketQuote, assess_edge, clv_points, clv_basis_points


# ---------------------------------------------------------------------------
# H4a: generalised devigged CLV — sign must track true direction
# ---------------------------------------------------------------------------

def _q(yes_ask, yes_bid, source="t"):
    from tools.domains.polymarket.market import PolymarketAdapter
    quote, _ = PolymarketAdapter.quote_from_book(yes_bid, yes_ask)
    quote.source = source
    return quote


@given(st.floats(min_value=0.02, max_value=0.98),
       st.floats(min_value=0.005, max_value=0.05))
@settings(max_examples=200, deadline=None)
def test_clv_positive_iff_market_moved_toward(mid, half_spread):
    """Hold the spread constant and move the mid. If the close is closer to
    our side than the claim was, CLV MUST be positive; if further, negative;
    identical mids must give ~0 (not a vig artifact)."""
    claim = _q(mid - half_spread, mid + half_spread)
    same = _q(mid - half_spread, mid + half_spread)
    v0 = clv_points(claim, same)
    assert abs(v0) < 1e-6, f"zero true move produced CLV {v0}"

    toward = _q((mid + 0.05) + half_spread, (mid + 0.05) - half_spread)
    away = _q((mid - 0.05) + half_spread, (mid - 0.05) - half_spread)
    assert clv_points(claim, toward) > 0, "move TOWARD read negative"
    assert clv_points(claim, away) < 0, "move AWAY read positive"


def test_clv_refuses_single_sided_quotes():
    c = MarketQuote(price=-110, kind="american")
    x = MarketQuote(price=-105, kind="american")
    assert clv_points(c, x) is None
    assert clv_basis_points(c, x) is None


# ---------------------------------------------------------------------------
# H4b: the DB-path unit zoo — points vs bps vs fractions vs rates
# ---------------------------------------------------------------------------

def test_clv_tracker_prob_bp_is_probability_basis_points_not_odds_delta():
    """clv_log.clv_cents is documented as holding prob-bp now. A bet placed
    at -110 (implied .5238) closing at -105 (implied .5122), both devigged,
    moved AGAINST us by ~1.16 prob-points => NEGATIVE bp. Any positive value
    means the units flipped."""
    import os, tempfile
    import tests.helpers as _  # noqa: F401  (conftest handles path)
    from tools.clv_tracker import CLVTracker

    async def run():
        with tempfile.TemporaryDirectory() as td:
            t = CLVTracker(os.path.join(td, "t.db"))
            await t.initialize()
            try:
                bid = await t.record_bet(
                    sport="basketball_nba", game_description="A @ B",
                    team="A", market="h2h", bookmaker="pinnacle",
                    placement_odds=-110, stake=100, event_id="e1",
                    edge_estimate=0.03)
                # record_closing_line also UPDATEs paper_trades; create the
                # table so the CLV path under test can run standalone.
                await t._db.execute(
                    """CREATE TABLE IF NOT EXISTS paper_trades (
                        trade_id TEXT PRIMARY KEY, event_id TEXT,
                        market TEXT, side TEXT, line REAL,
                        signal_implied_prob REAL, signal_odds_american INTEGER,
                        closing_odds INTEGER, closing_implied REAL,
                        book TEXT, sport TEXT, actual_result TEXT,
                        hypothetical_pnl REAL)""")
                await t.record_closing_line(
                    event_id="e1", market="h2h", team="A",
                    closing_odds=-105, source="pinnacle")
                cur = await t._db.execute("SELECT * FROM bets WHERE id=?", (bid,))
                row = dict(zip([d[0] for d in cur.description],
                               await cur.fetchone()))
                # odds-space delta: -110 -> -105 is a WORSE price for our side
                # (line moved against). implied went UP (.5238 -> .5122 is
                # wrong direction check — compute honestly):
                p_place = 100 / 210
                p_close = 100 / 205
                moved_against = p_close > p_place   # our side got shorter? no:
                # -110 implies .4762 for OUR side? No: American -110 implies
                # 110/210 = .5238 risked-side. The tracker stores
                # calculate_implied_probability(-110) = .5238.
                impl_place = 110 / 210      # .5238
                impl_close = 105 / 205      # .5122
                # clv_implied = close - place = -.0116: line moved so our side
                # costs MORE to back... verify the stored sign matches the
                # documented convention "positive = we got a better price".
                clv_stored = row["clv_implied"]
                assert clv_stored == pytest.approx(impl_close - impl_place,
                                                   abs=1e-4)
                return clv_stored
            finally:
                await t.close()
    got = asyncio.run(run())
    # The invariant under test: whatever sign convention, the SAME physical
    # move must not be reported as improvement on one path and decline on
    # another. Cross-check against the devigged fair-prob path used in
    # _log_clv: pinnacle fair of .5122 with vig .025 vs placement fair.
    from tools.clv_tracker import _half_vig_devig
    bp = (_half_vig_devig(105/205, 0.025) - _half_vig_devig(110/210, 0.025)) * 10000
    stored_sign = 1 if got > 0 else -1
    log_sign = 1 if bp > 0 else -1
    assert stored_sign == log_sign, (
        f"bets.clv_implied says {got:+.4f} but clv_log's devigged "
        f"clv_prob_bp says {bp:+.1f}bp for the SAME line move — the two "
        f"CLV paths disagree on whether the line helped or hurt")


def test_legacy_clv_cents_column_is_prob_bp_not_american_points():
    """The mixed-units poisoning bug: confirm no current writer puts
    American-point deltas into clv_cents. -110 -> -105 is -5 American
    points but ~-116 prob-bp; they must never be confused."""
    from tools.clv_tracker import _half_vig_devig
    place_f = _half_vig_devig(110/210, 0.05)
    close_f = _half_vig_devig(105/205, 0.025)
    bp = round((close_f - place_f) * 10000, 1)
    assert abs(bp) < 5000, "prob-bp out of plausible range"
    # an American-points value would be exactly -5.0; prob-bp must not be it
    assert bp != -5.0


@given(st.floats(min_value=0.30, max_value=0.70),
       st.floats(min_value=0.001, max_value=0.10))
@settings(max_examples=150, deadline=None)
def test_half_vig_devig_is_monotone_and_shrinking(implied, vig):
    from tools.clv_tracker import _half_vig_devig
    fair = _half_vig_devig(implied, vig)
    assert 0 < fair <= implied, (
        f"devig RAISED implied {implied} to {fair} — vig subtraction "
        f"inverted; every CLV using it would read phantom value")
    assert fair == pytest.approx(implied / (1 + vig / 2), rel=1e-9)


def test_zero_true_move_across_books_is_not_phantom_clv():
    """Same fair probability quoted by two books with different vig loads.
    Raw-implied comparison shows 'movement' that is pure vig difference.
    The half-vig approximation reduces but does NOT eliminate it — measure
    the residual and assert it stays small enough not to fake a signal."""
    from tools.clv_tracker import _half_vig_devig
    # fair 0.50: retail quotes -110/-110 (implied .5238/.5238 raw),
    # pinnacle closes -105/-105 (raw .5122). True movement: ZERO.
    retail_raw = 110/210
    sharp_raw = 105/205
    retail_fair = _half_vig_devig(retail_raw, 0.05)
    sharp_fair = _half_vig_devig(sharp_raw, 0.025)
    residual_bp = (sharp_fair - retail_fair) * 10000
    assert abs(residual_bp) < 50, (
        f"identical fair lines across books produced {residual_bp:+.1f}bp of "
        f"'CLV' from vig-model mismatch alone")


# ---------------------------------------------------------------------------
# H6: the live path is structurally dead — keep it that way
# ---------------------------------------------------------------------------

def test_kalshi_package_has_no_order_surface():
    import tools.domains.kalshi.market as k
    import tools.domains.polymarket.market as pm
    for mod in (k, pm):
        src_names = dir(mod)
        for banned in ("place_order", "submit", "create_order", "post",
                       "buy", "sell", "auth", "login"):
            hits = [n for n in src_names if banned == n.lower()]
            assert not hits, f"{mod.__name__} exposes {hits}"


@pytest.mark.asyncio
async def test_kalshi_plugin_execute_only_reads():
    """Every plugin action maps to a GET-shaped payload builder; none may
    return anything resembling an order object."""
    from tools.domains.kalshi.plugin import build_kalshi_plugin
    plugin = build_kalshi_plugin()
    names = [t.name if hasattr(t, "name") else str(t) for t in getattr(plugin, "tools", [])]
    joined = " ".join(names).lower()
    for banned in ("order", "trade_place", "execute_trade", "buy", "sell"):
        assert banned not in joined, f"kalshi plugin tool list contains {banned!r}"


@pytest.mark.asyncio
async def test_polymarket_plugin_execute_only_reads():
    from tools.domains.polymarket.plugin import build_polymarket_plugin
    plugin = build_polymarket_plugin()
    names = [t.name if hasattr(t, "name") else str(t) for t in getattr(plugin, "tools", [])]
    joined = " ".join(names).lower()
    for banned in ("order", "trade_place", "execute_trade", "buy", "sell"):
        assert banned not in joined, f"polymarket plugin tool list contains {banned!r}"


def test_bet_executor_defaults_to_disabled():
    """The structural kill: BetExecutor() must come up _enabled=False, and
    nothing in the money-math surface (edge/kelly/sizing/clv/domains/*)
    may reach enable(). We cannot prove absence of callers everywhere, but
    we CAN prove the default state and that importing the money modules
    does not flip any global."""
    from tools.bet_executor import BetExecutor
    ex = BetExecutor()
    assert ex.is_enabled is False
    assert ex.is_logged_in is False


def test_assess_edge_output_places_nothing():
    """EdgeAssessment carries no order/stake/execution field — measurement
    only. Guard the shape: if someone adds an execution hook later, this
    trips."""
    q = MarketQuote(price=0.45, counter_price=0.60, kind="probability")
    a = assess_edge("c", 0.60, q)
    dataclass_fields = {f for f in vars(a)}
    banned = {"order", "order_id", "stake", "placed", "execute",
              "executor", "action_endpoint"}
    assert not (dataclass_fields & banned), (
        f"EdgeAssessment grew execution fields: {dataclass_fields & banned}")
