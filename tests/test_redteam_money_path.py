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
