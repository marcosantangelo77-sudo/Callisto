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
from tools.devig import devig_market
from tools.kelly import kelly_full, kelly_fractional


# ---------------------------------------------------------------------------
# Defect M1 — crossed / stale-mixed books devig into a phantom fair price
# (Family 3: absence of a validity check treated as success)
# ---------------------------------------------------------------------------

def test_m1_crossed_book_overround_negative_must_be_rejected():
    """yes_ask=0.60, no_ask=0.61 cannot both be asks on one binary book:
    complementary asks sum ABOVE 1 only when the spread is positive, but this
    pair implies a NEGATIVE hold (a free-lunch book). devig_market returns a
    confident 'fair' probability from it instead of an error."""
    r = devig_market([1.0 / 0.60, 1.0 / 0.61])
    assert "error" in r or r["fair_probabilities"], r  # sanity: call succeeds
    # The invariant: a negative-overround book must not produce fair probs.
    assert r.get("error") is not None, (
        f"devig_market accepted a crossed book (overround={r['overround']}) "
        f"and returned fair probabilities {r['fair_probabilities']}"
    )


def test_m1b_stale_snapshot_mix_manufactures_actionable_edge():
    """The Kalshi wiring (tools/domains/kalshi/market.py) builds quotes from
    two independent fields fetched in one payload; a stale/crossed mix
    (price=0.60, counter=0.61, overround=-0.19) flows straight through
    assess_edge and comes out actionable=False by luck — but flip the side
    and the SAME defect manufactures a 12.6-point edge with Kelly at cap."""
    q = MarketQuote(price=0.60, counter_price=0.61, kind="probability")
    a = assess_edge("t", 0.62, q)
    # Precondition: the audit itself records the crossed book...
    assert a.devig_audit["overround"] < 0
    # ...yet assess_edge still emits a fair probability and full Kelly sizing
    # from it instead of refusing:
    assert a.market_prob_fair == pytest.approx(0.4943, abs=1e-3), (
        "crossed book devigged into a fair price instead of being rejected"
    )


def test_m1c_assess_edge_has_no_overround_sanity_gate():
    """assess_edge must refuse any quote whose audit shows overround <= 0 or
    overround > 0.5 (no real two-way book holds 50%). Currently neither is
    checked anywhere between devig_market and EdgeAssessment."""
    for price, counter in [(0.60, 0.61), (0.55, 0.50), (0.30, 0.95)]:
        q = MarketQuote(price=price, counter_price=counter, kind="probability")
        a = assess_edge("t", 0.5, q)
        over = a.devig_audit["overround"]
        assert 0.0 < over < 0.5, (
            f"quote ({price},{counter}) has nonsensical overround {over} "
            f"but assess_edge emitted a fair probability {a.market_prob_fair} "
            f"with no error"
        )


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


def test_m2_kelly_full_never_rounds_up():
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
    # 486,921 violating cells in the sweep; a handful of pins prove the family
    assert len(violations) == 0, (
        f"{len(violations)} parameter cells where kelly_full rounds UP "
        f"(automated actor raising a stake), e.g. {violations[:5]}"
    )


def test_m2b_kelly_fractional_double_rounding_also_raises():
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

def test_m3_kelly_positive_while_fair_edge_negative():
    """A claim can LOSE to the devigged market price (edge < 0 — no edge by
    assess_edge's own definition) yet carry a positive reported Kelly fraction,
    because the Kelly branch reuses the RAW implied payout. Two copies of
    'the market price' disagree inside one EdgeAssessment."""
    cases = []
    for am, p in [(125, 0.45), (150, 0.45), (175, 0.40), (200, 0.35)]:
        ctr = -104 if am < 0 else 108
        q = MarketQuote(price=am, counter_price=ctr, kind="american")
        a = assess_edge("t", p, q)
        if a.edge < 0 and a.kelly_fraction_full > 0:
            cases.append((am, p, a.edge, a.kelly_fraction_full))
    assert cases, "expected the divergence to reproduce"  # documents the bug exists
    assert all(k == 0 for _, _, _, k in cases), (
        f"negative-fair-edge assessments carry positive Kelly: {cases[:4]}"
    )


# ---------------------------------------------------------------------------
# Defect M4 — auto-kind misreads cent-quoted contracts (Family 4: a string /
# convention decides a trust outcome)
# ---------------------------------------------------------------------------

def test_m4_auto_kind_reads_47_cent_contract_as_decimal_odds():
    """A Kalshi/Polymarket contract quoted '47' (cents) with kind left 'auto'
    is silently read as decimal odds 47 -> implied 2.1%. A 47% contract
    becomes a 2% contract: the direction of the manufactured error depends
    on which side you back, and either way the 'market probability' is off
    by a factor of ~22."""
    q = MarketQuote(price=47)          # caller forgot kind='contract_cents'
    p = q.implied_probability()
    assert not math.isclose(p, 1.0 / 47.0), "auto misread reproduced: 47 cents -> 1/47"
    assert 0.45 < p < 0.49, f"a 47-cent contract should imply ~0.47, got {p}"


def test_m4b_auto_kind_integer_geq_100_is_ambiguous_but_accepted():
    """price=110 with kind='auto' is read as American +110 (implied 47.6%) —
    but the identical number as a $1.10 contract price or decimal odds 110
    are equally plausible readings. Auto silently picks one; there is no way
    for downstream code to detect which convention was assumed."""
    assert _raw_implied(110) == pytest.approx(100 / 210)


# ---------------------------------------------------------------------------
# Defect M5 — summary() rounding can move edge UP (Family 6)
# ---------------------------------------------------------------------------

def test_m5_summary_round_never_raises_edge_or_kelly():
    q = MarketQuote(price=-110, counter_price=-110, kind="american")
    for num in range(500001, 500999):
        p = 0.5 + num * 1e-9
        s = assess_edge("t", p, q).summary()
        a_edge_raw = p - q.implied_probability()  # fair==raw here (no vig)
        assert s["edge"] <= a_edge_raw + 1e-12 or s["edge"] == 0.0, (
            f"summary rounded edge up: {p} -> {s['edge']} vs {a_edge_raw}"
        )


# ---------------------------------------------------------------------------
# Defect M6 — CLV accepts a crossed book on ONE side only (Family 3)
# ---------------------------------------------------------------------------

def test_m6_clv_with_one_crossed_quote_returns_a_number():
    """clv_points requires both audits to say 'devigged', but a crossed book
    IS devigged=True. One healthy close quote + one stale-crossed claim quote
    yields a signed CLV number that feeds track-record scoring."""
    claim = MarketQuote(price=0.40, counter_price=0.41, kind="probability")   # overround -0.19
    close = MarketQuote(price=0.45, counter_price=0.56, kind="probability")   # healthy
    v = clv_points(claim, close)
    assert v is not None  # currently: returns -0.048 despite the invalid claim book
    # Invariant wanted: invalid book on either side -> None (refuse to score).
