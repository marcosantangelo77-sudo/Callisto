"""REDTEAM — money path (devig / edge / Kelly / CLV). READ-ONLY: these tests
attack the arithmetic; nothing here arms execution.

Surface: money path — unattacked ground (surfaces already covered: calibration,
checkpoint/resume x4, concurrency, loop, pipeline_wiring, provenance, seal,
synthesis, retrieval).

Method: PROPERTY-BASED SWEEP over the full parameter space of the money
arithmetic, plus adversarial inputs at boundaries. Chosen because
PATTERNS.md ranks property sweeps the single most productive method here and
no prior pass used one on this surface; hand-written fixtures verified every
formula on well-formed two-way books and never touched crossed books, negative
overround, kind mismatches, or rounding direction.

Families hunted:
  Family 3 (absence treated as success): a book whose sides do not belong to
    the same market/snapshot — overround <= 0 or absurdly large — is devigged
    as happily as a healthy one.
  Family 6 (direction of error): round() raising Kelly fractions; summary()
    rounding edge upward.
  Family 2 (same rule in two copies): assess_edge computes edge against the
    DEVIGGED probability but Kelly/EV against the RAW decimal payout — two
    copies of "the price" that disagree.

Each defect below was first found by random sweep, then pinned to a minimal
reproducing case.
"""

import math

import pytest

from tools.edge import (
    MAX_FRACTION_FULL_KELLY,
    MIN_EDGE_TO_ACT,
    MarketQuote,
    _raw_implied,
    assess_edge,
    clv_points,
)
from tools.devig import devig_american, devig_market
from tools.math_utils import american_to_decimal
from tools.kelly import kelly_full, kelly_fractional


# ---------------------------------------------------------------------------
# REPAIRED (was M1) — invalid two-sided books are rejected before sizing
# (Family 3: absence of a validity check treated as success)
#
# Quote convention note: these probes use complementary ASKS. Buying YES at
# its ask and NO at its ask must sum to 1 + spread, so a healthy book has a
# small POSITIVE overround. asks 0.60/0.61 imply probs summing to 1.21 — an
# overround of +0.21 (a 17.4% hold), NOT negative; that is a stale snapshot
# mix / absurd hold, not a crossed book. A crossed book (asks summing BELOW
# 1, e.g. 0.45/0.50) has NEGATIVE overround. Both classes must be rejected.
# ---------------------------------------------------------------------------

def test_repaired_malformed_and_nonfinite_books_are_rejected():
    """Non-finite, non-numeric, and degenerate odds must return an error audit,
    never fair probabilities."""
    bad_books = [
        [float("nan"), 2.0],
        [float("inf"), 2.0],
        [1.0, "not-a-number"],
        [None, 2.0],
        [1.0, 1.0],          # decimal odds of 1.0 -> implied prob of exactly 1
        [0.5, 3.0],          # decimal odds below 1 -> implied prob above 1
    ]
    for odds in bad_books:
        r = devig_market(odds)
        assert r.get("error"), f"devig_market accepted malformed book {odds}: {r}"
        assert r.get("fair_probabilities") == []


def test_repaired_crossed_book_negative_overround_is_rejected():
    """Crossed asks (sum below 1 -> negative overround, free-lunch book) must
    produce an error audit, not a confident fair price."""
    r = devig_market([1.0 / 0.45, 1.0 / 0.50])   # implied sum 0.95, overround -0.05
    assert r.get("error") is not None, (
        f"devig_market accepted a crossed book (overround={r['overround']}) "
        f"and returned fair probabilities {r['fair_probabilities']}"
    )
    assert r["fair_probabilities"] == []
    assert r["overround"] < 0


def test_repaired_implausible_hold_stale_mix_is_rejected():
    """yes_ask=0.60, no_ask=0.61 cannot both be asks on one live binary book:
    implied probabilities sum to 1.21, an overround of +0.21 (a ~17% hold).
    Real retail books hold 2-8%; this is a stale/mismatched mix and must be
    rejected rather than devigged into a precise-looking fair probability."""
    r = devig_market([1.0 / 0.60, 1.0 / 0.61])
    assert r["overround"] > 0, (
        f"quote convention error in the test itself: asks 0.60/0.61 imply "
        f"overround {r['overround']}, which is positive"
    )
    assert r.get("error") is not None, (
        f"devig_market accepted an absurd-hold stale mix "
        f"(overround={r['overround']}) and returned {r['fair_probabilities']}"
    )
    assert r["fair_probabilities"] == []


def test_repaired_valid_binary_books_still_devig():
    """Valid controls: healthy probability, American-odds and contract books
    keep working through the gate."""
    # Probability convention, retail-like ~4.8% hold.
    ok = devig_market([1.0 / 0.52, 1.0 / 0.52])
    assert "error" not in ok
    assert ok["method"] == "power"
    assert abs(sum(ok["fair_probabilities"]) - 1.0) < 1e-6

    # American convention: standard -110/-110 book.
    am = devig_american(-110, -110)
    assert "error" not in am
    assert am["side_a"]["fair_prob"] == pytest.approx(0.5)

    # Slightly wide but live: -105/-105 (~2.4% hold).
    plaus = devig_american(-105, -105)
    assert "error" not in plaus
    assert all(0 < p < 1 for p in plaus["fair_probabilities"])
    assert plaus["overround"] > 0


def test_repaired_assess_edge_fails_safely_on_invalid_book():
    """assess_edge keeps its public shape on an invalid book but the assessment
    must be inert: no fair probability, no edge, no Kelly stake, not actionable."""
    q = MarketQuote(price=0.60, counter_price=0.61, kind="probability")
    a = assess_edge("t", 0.62, q)
    assert isinstance(a.market_prob_raw, float)
    assert not math.isfinite(a.market_prob_fair)
    assert not math.isfinite(a.edge)
    assert a.kelly_fraction_full == 0.0
    assert a.kelly_fraction_quarter == 0.0
    assert not a.actionable
    assert not a.devig_audit.get("devigged")
    assert a.devig_audit.get("invalid_book")


def test_repaired_no_invalid_audit_can_yield_positive_sizing():
    """Property sweep: every quote whose audit records an invalid book must
    carry zero Kelly fractions and actionable=False, regardless of how large
    the calibrated probability claims the edge to be."""
    invalid_quotes = [
        MarketQuote(price=0.60, counter_price=0.61, kind="probability"),   # +0.21 hold
        MarketQuote(price=0.40, counter_price=0.41, kind="probability"),   # crossed
        MarketQuote(price=-200, counter_price=-200, kind="american"),      # both favs
        MarketQuote(price=float("nan"), counter_price=0.55, kind="probability"),
    ]
    for q in invalid_quotes:
        for p in [0.51, 0.75, 0.99]:
            a = assess_edge(f"t-{q.price}-{p}", p, q)
            assert a.kelly_fraction_full == 0.0, (q, p, a.summary())
            assert a.kelly_fraction_quarter == 0.0
            assert not a.actionable
            assert a.devig_audit.get("invalid_book") or not a.devig_audit.get("devigged"), (q, p)


# ---------------------------------------------------------------------------
# Defect M2 — round() raises the Kelly fraction (Family 6: direction of error)
# Property: kelly_full() may never exceed the exact unrounded fraction.
# ---------------------------------------------------------------------------

def _exact_kelly(edge: float, american: int) -> float:
    dec = 1.0 + (american / 100.0 if american > 0 else 100.0 / abs(american))
    b = dec - 1.0
    implied = 100.0 / (american + 100.0) if american > 0 else abs(american) / (abs(american) + 100.0)
    p = min(1.0, max(0.0, implied + edge))
    return max(0.0, (b * p - (1.0 - p)) / b)


def test_repaired_m2_kelly_full_never_rounds_up():
    violations = []
    americans = list(range(-2000, -99)) + list(range(100, 10001))
    edges = [x / 10_000 for x in range(5, 500, 5)]
    for am in americans:
        exact = None
        for e in edges:
            ex = _exact_kelly(e, am)
            got = kelly_full(e, am)
            if got > ex + 1e-12:
                violations.append((e, am, got, ex))
    # Previously 486,921 violating cells; kelly_full now floors instead of rounding.
    assert len(violations) == 0, (
        f"{len(violations)} parameter cells where kelly_full rounds UP "
        f"(automated actor raising a stake), e.g. {violations[:5]}"
    )


def test_repaired_m2b_kelly_fractional_double_rounding_also_raises():
    violations = 0
    for am in list(range(-2000, -99)) + list(range(100, 5001)):
        for e in [x / 10_000 for x in range(5, 300, 5)]:
            if kelly_fractional(e, am) > _exact_kelly(e, am) / 4.0 + 1e-12:
                violations += 1
    assert violations == 0, (
        f"{violations} cells where quarter-Kelly exceeds exact/4 after "
        f"double rounding (kelly_full then kelly_fractional)"
    )


# ---------------------------------------------------------------------------
# Defect M3 — edge measured against devigged fair, Kelly/EV computed at raw
# decimal payout (Family 2: the same rule in two disagreeing copies)
# ---------------------------------------------------------------------------

def test_repaired_m3_no_positive_kelly_when_fair_edge_negative():
    """A claim can LOSE to the devigged market price (edge < 0 — no edge by
    assess_edge's own definition) yet carry a positive reported Kelly fraction,
    because the Kelly branch reuses the RAW implied payout. Two copies of
    'the market price' disagree inside one EdgeAssessment."""
    cases = []
    for am, p in [(125, 0.45), (150, 0.45), (175, 0.40), (200, 0.35)]:
        ctr = -104 if am < 0 else 108
        q = MarketQuote(price=am, counter_price=ctr, kind="american")
        a = assess_edge("t", p, q)
        cases.append((am, p, a.edge, a.kelly_fraction_full))
    # Invariant: negative fair edge => zero Kelly stake.
    assert all(k == 0.0 for _, _, _, k in cases), (
        f"negative-fair-edge assessments carry positive Kelly: {cases[:4]}"
    )


# ---------------------------------------------------------------------------
# Defect M4 — auto-kind misreads cent-quoted contracts (Family 4: a string /
# convention decides a trust outcome)
# ---------------------------------------------------------------------------

def test_repaired_m4_auto_kind_refuses_ambiguous_integral_price():
    """A Kalshi/Polymarket contract quoted '47' (cents) with kind left 'auto'
    is silently read as decimal odds 47 -> implied 2.1%. A 47% contract
    becomes a 2% contract: the direction of the manufactured error depends
    on which side you back, and either way the 'market probability' is off
    by a factor of ~22."""
    # Auto-kind resolves an integral price in [2, 100) as a cent-quoted
    # CONTRACT (repo convention; decimal odds are quoted fractional, e.g. 1.91),
    # so a 47-cent contract can never silently become a 2% decimal-odds read:
    assert MarketQuote(price=47).implied_probability() == pytest.approx(0.47)
    assert not math.isclose(MarketQuote(price=47).implied_probability(), 1.0 / 47.0)
    # Explicit kinds keep their exact meanings:
    assert MarketQuote(price=47, kind="contract_cents").implied_probability() == pytest.approx(0.47)
    assert MarketQuote(price=47, kind="decimal").implied_probability() == pytest.approx(1 / 47)


def test_m4b_auto_kind_integer_geq_100_is_ambiguous_but_accepted():
    """price=110 with kind='auto' is read as American +110 (implied 47.6%) —
    but the identical number as a $1.10 contract price or decimal odds 110
    are equally plausible readings. Auto silently picks one; there is no way
    for downstream code to detect which convention was assumed."""
    assert _raw_implied(110) == pytest.approx(100 / 210)


# ---------------------------------------------------------------------------
# Defect M5 — summary() rounding can move edge UP (Family 6)
# ---------------------------------------------------------------------------

def test_repaired_m5_summary_round_never_raises_edge_or_kelly():
    q = MarketQuote(price=-110, counter_price=-110, kind="american")
    for num in range(500001, 500999):
        p = 0.5 + num * 1e-9
        a = assess_edge("t", p, q)
        s = a.summary()
        assert s["kelly_full"] <= a.kelly_fraction_full + 1e-12 and \
            s["kelly_quarter"] <= a.kelly_fraction_quarter + 1e-12, (
            f"summary rounded Kelly up: {p} -> {s['kelly_full']}/"
            f"{s['kelly_quarter']} vs {a.kelly_fraction_full}/{a.kelly_fraction_quarter}"
        )


# ---------------------------------------------------------------------------
# Defect M6 — CLV accepts a crossed book on ONE side only (Family 3)
# ---------------------------------------------------------------------------

def test_repaired_m6_clv_refuses_invalid_book_on_either_side():
    """clv_points requires both audits to be cleanly devigged; a crossed or
    absurd-hold book on EITHER side must yield None, never a signed number."""
    claim_bad = MarketQuote(price=0.40, counter_price=0.41, kind="probability")  # crossed
    close_ok = MarketQuote(price=0.45, counter_price=0.56, kind="probability")   # healthy
    assert clv_points(claim_bad, close_ok) is None
    assert clv_points(close_ok, claim_bad) is None
    hold_bad = MarketQuote(price=0.60, counter_price=0.61, kind="probability")   # +0.21 hold
    assert clv_points(hold_bad, close_ok) is None
    assert clv_points(close_ok, hold_bad) is None


# ---------------------------------------------------------------------------
# Blocker 1 — zero/non-positive overround must be inert end-to-end
# ---------------------------------------------------------------------------

def test_blocker1_zero_overround_book_is_inert():
    """A zero-hold book (.5/.5 probability) must never devig into an
    actionable fair price or positive Kelly."""
    r = devig_market([2.0, 2.0])          # implied sum exactly 1 -> hold 0
    assert r.get("error"), f"zero-hold book accepted: {r}"
    assert r["fair_probabilities"] == []

    q = MarketQuote(price=0.5, counter_price=0.5, kind="probability")
    a = assess_edge("t", 0.99, q)         # huge claimed edge must not matter
    assert not a.actionable
    assert a.kelly_fraction_full == 0.0
    assert a.kelly_fraction_quarter == 0.0
    assert not math.isfinite(a.market_prob_fair)
    assert not math.isfinite(a.edge)
    assert a.devig_audit.get("invalid_book")
    # invalid audit state is distinct from a clean no-edge result
    clean = assess_edge("t", 0.50, MarketQuote(price=-110, counter_price=-110,
                                               kind="american"))
    assert clean.devig_audit.get("devigged") and not clean.actionable


def test_blocker2_invalid_american_quotes_never_coerced():
    """kind='american' values are validated as American odds; out-of-policy
    (50, 100) and fractional (100.9, -110) quotes are inert, never trusted."""
    for price, ctr in [(50, 100), (100.9, -110), (0, -110), (True, -110),
                       (float("nan"), -110)]:
        q = MarketQuote(price=price, counter_price=ctr, kind="american")
        with pytest.raises(ValueError):
            q.implied_probability()
        a = assess_edge("t", 0.9, q)
        assert not a.actionable
        assert a.kelly_fraction_full == 0.0
        assert a.devig_audit.get("invalid_book")


def test_blocker2_healthy_american_still_works():
    q = MarketQuote(price=-105, counter_price=-105, kind="american")
    f, aud = q.fair_probability()
    assert aud.get("devigged") and abs(f - 0.5) < 0.01


def test_blocker3_convenience_helpers_route_through_the_gate():
    """devig_pinnacle / devig_retail cannot bypass the market-sanity gate:
    paired favorite odds (-200, -200) have a 33% hold and must be rejected."""
    from tools.devig import devig_pinnacle, devig_retail
    for fn in (devig_pinnacle, devig_retail):
        with pytest.raises(ValueError):
            fn(-200, -200)
        # Healthy controls still devig.
        a, b = fn(-110, -110)
        assert abs(a + b - 1.0) < 1e-6


def test_blocker4_explicit_decimal_kind_is_honoured_by_payout():
    """Decimal odds 2/1.98 is a real book (~0.5% hold). assess_edge must use
    payout b=1.0 (decimal 2), NOT reinterpret '2' as a 2-cent contract."""
    q = MarketQuote(2, 1.98, kind="decimal")
    f, aud = q.fair_probability()
    assert aud.get("devigged")
    assert abs(f - 0.497487) < 1e-3

    a = assess_edge("claim-d", 0.55, q)
    assert a.actionable
    # b = decimal - 1 = 1.0 -> Kelly = edge/b = ~0.0525 capped path, EV = 0.10
    assert a.ev_per_unit == pytest.approx(0.55 * 1.0 - 0.45)
    expected_kelly = min((1.0 * 0.55 - 0.45) / 1.0, MAX_FRACTION_FULL_KELLY)
    assert a.kelly_fraction_full == pytest.approx(expected_kelly)
    assert a.kelly_fraction_full < 0.15     # nowhere near the bogus maximum


def test_blocker4_contract_cents_kind_keeps_cent_payout():
    """Symmetric control: an explicit cent-quoted book converts its payout in
    cents (47 -> decimal 1/0.47), preserving prior behavior."""
    q = MarketQuote(54, 54, kind="contract_cents")
    a = assess_edge("claim-c", 0.60, q)
    dec = 1.0 / 0.54
    assert a.ev_per_unit == pytest.approx(0.60 * (dec - 1.0) - 0.40)


def test_all_kinds_healthy_controls_assess_end_to_end():
    """Every supported convention keeps working through assess_edge."""
    quotes = [
        MarketQuote(price=-110, counter_price=-110, kind="american"),
        MarketQuote(price=1.95, counter_price=1.95, kind="decimal"),
        MarketQuote(price=0.52, counter_price=0.52, kind="probability"),
        MarketQuote(price=53, counter_price=53, kind="contract_cents"),
        MarketQuote(price=47, counter_price=54),   # auto integral contract
    ]
    for q in quotes:
        a = assess_edge(f"ok-{q.kind}", 0.60, q)
        assert a.devig_audit.get("devigged"), (q.kind, a.summary())
        assert 0 < a.market_prob_fair < 1
        assert a.edge > 0 and a.actionable
        assert 0 <= a.kelly_fraction_full <= MAX_FRACTION_FULL_KELLY


def test_explicit_probability_out_of_range_raises_and_is_inert():
    q = MarketQuote(price=1.5, counter_price=0.7, kind="probability")
    with pytest.raises(ValueError):
        q.implied_probability()


# ---------------------------------------------------------------------------
# Blocker R2-1 — kelly_full must not coerce invalid American odds
# ---------------------------------------------------------------------------

def test_r2_kelly_full_rejects_invalid_american_odds():
    """Fractional quotes like 100.9 and booleans must never be int()-coerced
    into a positive stake."""
    assert kelly_full(0.40, 100.9) == 0.0
    assert kelly_full(0.40, True) == 0.0
    assert kelly_full(0.40, False) == 0.0
    assert kelly_fractional(0.40, 100.9) == 0.0
    assert kelly_fractional(0.40, True) == 0.0
    # Out-of-policy magnitudes and non-finite values likewise
    for bad in (50, -50, 0, float("nan"), float("inf"), "110", None):
        assert kelly_full(0.40, bad) == 0.0


def test_r2_kelly_full_healthy_controls_unchanged():
    """Valid American odds keep their exact Kelly semantics after validation."""
    assert kelly_full(0.05, -110) == pytest.approx(0.105, abs=1e-6)
    assert kelly_full(0.05, 100) > 0
    assert kelly_full(-0.10, -110) == 0.0
    assert kelly_full(0.60, -110) == 1.0   # upper clamp p=1 preserved


# ---------------------------------------------------------------------------
# Blocker R2-2 — no_vig_price / devig_multiplicative route through the gate
# ---------------------------------------------------------------------------

def test_r2_no_vig_price_high_hold_book_is_rejected():
    """no_vig_price(-200, -200) is a 33%-hold book; it must raise, never
    return a confident (0.5, 0.5)."""
    from tools.math_utils import no_vig_price
    with pytest.raises(ValueError):
        no_vig_price(-200, -200)
    from tools.boost_evaluator import devig_multiplicative
    with pytest.raises(ValueError):
        devig_multiplicative(-200, -200)
    # Healthy control keeps working through the same path.
    a, b = no_vig_price(-110, -110)
    assert abs(a + b - 1.0) < 1e-9


# ---------------------------------------------------------------------------
# Blocker R2-3 — low-level devig helpers share the market-sanity gate
# ---------------------------------------------------------------------------

def test_r2_low_level_devig_helpers_share_the_gate():
    """Every exported devig helper rejects zero-hold and crossed books."""
    from tools.devig import (
        additive_devig,
        multiplicative_devig,
        power_devig,
        shin_devig,
    )
    zero_hold = [2.0, 2.0]                       # overround exactly 0
    crossed = [1.0 / 0.45, 1.0 / 0.50]           # overround -0.05
    stale_mix = [1.0 / 0.60, 1.0 / 0.61]         # +21-point overround
    malformed = [float("nan"), 2.0]
    for bad in (zero_hold, crossed, stale_mix, malformed):
        for fn in (multiplicative_devig, additive_devig):
            with pytest.raises(ValueError):
                fn(bad)
        for fn in (power_devig, shin_devig):
            with pytest.raises(ValueError):
                fn(bad)
    # Healthy controls: all helpers still devig a live ~2.5% hold book.
    ok = [1.95, 1.98]
    m = multiplicative_devig(ok)
    assert all(p > 0 for p in m)
    p, k = power_devig(ok)
    assert all(0 < x < 1 for x in p) and k >= 1.0
    s, z = shin_devig(ok)
    assert all(0 < x < 1 for x in s)


# ---------------------------------------------------------------------------
# Blocker R2-4 — devig_pinnacle stays multiplicative (documented identity)
# ---------------------------------------------------------------------------

def test_r2_devig_pinnacle_remains_multiplicative():
    """devig_pinnacle(-145, +125) must equal an explicitly multiplicative
    devig of the same book, not drift to the auto-selected power method."""
    from tools.devig import devig_market, devig_pinnacle
    a, b = devig_pinnacle(-145, 125)
    ref = devig_market(
        [american_to_decimal(-145), american_to_decimal(125)],
        method="multiplicative",
    )
    assert "error" not in ref
    assert a == pytest.approx(ref["fair_probabilities"][0], abs=1e-12)
    assert b == pytest.approx(ref["fair_probabilities"][1], abs=1e-12)
    assert ref["method"] == "multiplicative"


def test_r2_devig_retail_keeps_power_identity():
    """devig_retail retains its documented power-method output."""
    from tools.devig import devig_market, devig_retail
    a, b = devig_retail(-200, 170)
    ref = devig_market(
        [american_to_decimal(-200), american_to_decimal(170)],
        method="power",
    )
    assert "error" not in ref
    assert a == pytest.approx(ref["fair_probabilities"][0], abs=1e-12)
    assert b == pytest.approx(ref["fair_probabilities"][1], abs=1e-12)


# ---------------------------------------------------------------------------
# Blocker R2-5 — boost_evaluator.devig_additive must share the gate too
# ---------------------------------------------------------------------------

def test_r2_boost_devig_additive_rejects_unsanitary_books():
    """devig_additive previously bypassed the market-sanity gate and returned
    confident (0.5, 0.5) for zero-hold books; it now raises like the rest."""
    from tools.boost_evaluator import devig_additive
    with pytest.raises(ValueError):
        devig_additive(-100, -100)      # zero hold
    with pytest.raises(ValueError):
        devig_additive(-200, -200)      # ~33% hold
    # Healthy control keeps its additive semantics.
    a, b = devig_additive(-110, -110)
    assert abs(a + b - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Review blockers — exact 20% hold boundary, direct Kelly input safety,
# and CLV None contract
# ---------------------------------------------------------------------------

def test_exact_twenty_pct_hold_is_invalid_via_assess_edge():
    """A book whose sides are each priced at exactly 1/0.6 has a mathematical
    overround of exactly 20% — at the configured ceiling, therefore invalid.
    Float representation sums the implied probabilities to
    0.19999999999999996, which previously slipped past the gate and produced
    an actionable assessment."""
    q = MarketQuote(price=1 / 0.6, counter_price=1 / 0.6, kind="decimal")
    a = assess_edge("exact-hold", 0.9, q)
    assert not a.actionable
    assert a.kelly_fraction_full == 0.0
    assert a.kelly_fraction_quarter == 0.0
    assert a.devig_audit.get("invalid_book")
    assert math.isnan(a.edge)

    # Healthy control just below the ceiling must stay actionable-capable.
    q_ok = MarketQuote(price=1 / 0.59, counter_price=1 / 0.6, kind="decimal")
    a_ok = assess_edge("below-ceiling", 0.9, q_ok)
    assert not a_ok.devig_audit.get("invalid_book")
    assert math.isfinite(a_ok.edge)


def test_kelly_full_rejects_nonfinite_and_bool_edges():
    """kelly_full(nan, -110) previously returned 1.0 (NaN comparisons never
    fire) and kelly_full(True, ...) treated bool as a probability. Invalid
    direct inputs must yield 0.0, never a stake."""
    for bad in (float("nan"), float("inf"), float("-inf"), True):
        assert kelly_full(bad, -110) == 0.0, f"bad edge accepted: {bad!r}"
    # Valid control unchanged.
    assert kelly_full(0.05, -110) > 0.0


def test_kelly_fractional_rejects_bad_multipliers():
    """A negative/non-finite/oversized fraction previously scaled a valid
    Kelly fraction into a negative or oversized stake; it now returns 0.0."""
    for bad in (-1, 0, 1.5, float("nan"), float("inf"), True):
        got = kelly_fractional(0.05, -110, bad)
        assert got == 0.0, f"bad fraction accepted: {bad!r} -> {got}"
    assert got <= kelly_full(0.05, -110)
    # Valid quarter-Kelly control unchanged.
    assert kelly_fractional(0.05, -110, 0.25) \
        == pytest.approx(kelly_full(0.05, -110) * 0.25)


def test_clv_points_returns_none_for_devig_impossible_quotes():
    """"clv_points documents None as its no-trusted-CLV signal; it previously
    raised ValueError when fair_probability leaked a nonfinite value."""
    from tools.devig import MAX_SANE_OVERROUND

    def quote_at(hold):
        p_yes = 1 / 0.55
        p_no = 1 / ((1 - 0.55) + hold)
        return MarketQuote(price=p_yes, counter_price=p_no, kind="decimal")

    claim_bad = quote_at(MAX_SANE_OVERROUND)     # at-ceiling book -> invalid
    close_ok = MarketQuote(price=0.55, counter_price=0.46, kind="probability")
    assert clv_points(claim_bad, close_ok) is None
    assert clv_points(close_ok, claim_bad) is None
    assert isinstance(clv_points(close_ok, close_ok), float)
