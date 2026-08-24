"""R5 — Edge quantification as a lifecycle stage.

The bridge from a sealed conclusion to a position:

    calibrated probability -> market-implied probability -> edge
        -> Kelly fraction -> position, with implied price recorded at claim time

The last step is CLV, generalised: the price at claim time is the benchmark
the position will later be graded against. The math is IDENTICAL for a sports
bet, a Kalshi/Polymarket contract, an options position, and a binary biotech
event — only the price SOURCE differs. So the input is a domain-general
price quote (MarketQuote) accepting:

  - American odds            (-110, +150)         sports books
  - decimal odds             (1.91)               exchanges, EU books
  - contract price           ($0.47, cents)       Kalshi / Polymarket
  - raw implied probability  (0.52)               anything normalised

and normalising to a DEVIGGED probability. Devigging is not optional: an
earlier audit found CLV computed raw-implied vs raw-implied with no devig
anywhere, baking a 1-4% phantom edge into every historical row. A one-sided
quote cannot be devigged alone — devigging removes overround, which only
exists across the market's sides — so MarketQuote takes an optional
counter-quote; with one supplied, the market's fair probabilities come from
tools/devig.py (already verified numerically). With none supplied the raw
implied probability is used and `devigged` is False, so downstream code can
refuse to call it a fair price.

SAFETY: this module computes and records. It places nothing. There is no
order routing, no network call, no write to any execution table. The output
is a dataclass (EdgeAssessment) for the lifecycle stage to consume.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional, Union

from tools.devig import devig_market
from tools.kelly import kelly_full

Quote = Union[int, float]


def _raw_implied(price: Quote) -> float:
    """Raw implied probability from any accepted price representation.

    AUTO-KIND RULE (unit confusion, Family 4): an integer in [2, 99] reads
    as a cent-quoted CONTRACT PRICE (Kalshi/Polymarket convention), NOT as
    decimal odds. Reading 47 as decimal odds implies 1/47 ≈ 2.1% when the
    contract says 47% — a ~22x error whose direction depends on which side
    you back. Callers quoting genuine integer decimal odds in 2..99 must
    declare kind='decimal'. Integers with |v| >= 100 are American odds by
    market convention.
    """
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
        raise ValueError(f"price must be a finite number, got {price!r}")

    if isinstance(price, int) and price != 0:
        if abs(price) >= 100:
            # American odds are integers with |value| >= 100 by convention.
            return _american_to_implied(price)
        if price >= 2:
            # Cent-quoted contract: 47 means $0.47.
            return price / 100.0
        raise ValueError(
            f"integer price {price} has no unambiguous reading under "
            f"kind='auto'; declare kind explicitly"
        )
    return _continuous_to_prob(float(price))


def _american_to_implied(american: int) -> float:
    if american > 0:
        return 100.0 / (american + 100.0)
    if american < 0:
        return (-american) / ((-american) + 100.0)
    raise ValueError("American odds of 0 are not a valid price")


def _continuous_to_prob(p: float) -> float:
    """Continuous representations: implied prob in [0,1], decimal odds > 1,
    or a contract price quoted in cents (e.g. 47 for $0.47)."""
    if 0.0 < p <= 1.0:
        return p                      # already a probability
    if p > 1.0:
        return 1.0 / p                # decimal odds
    raise ValueError(
        f"cannot interpret {p!r} as a probability or decimal odds; "
        f"use kind='contract_cents' for cent-quoted contracts"
    )


@dataclass
class MarketQuote:
    """A market price at prediction time, from ANY domain.

    Exactly one of the price fields is set per side; the counter side is
    what enables devigging.
    """

    price: Quote                       # our side
    counter_price: Optional[Quote] = None   # other side of the same market
    kind: str = "auto"                 # auto | american | decimal | contract_cents | probability
    source: str = ""                   # e.g. "polymarket", "pinnacle", "kalshi"
    as_of: str = ""                    # ISO timestamp the quote was live

    def implied_probability(self) -> float:
        if self.kind == "auto":
            return _raw_implied(self.price)
        return {
            "american": lambda: _american_to_implied(int(self.price)),
            "decimal": lambda: 1.0 / float(self.price),
            "contract_cents": lambda: float(self.price) / 100.0,
            "probability": lambda: float(self.price),
        }[self.kind]()

    def fair_probability(self) -> tuple[float, dict]:
        """Devigged probability plus the audit trail of how it was derived.

        With a counter-quote: two-way devig via tools/devig.py (auto method).
        Without: raw implied, flagged devigged=False — callers must treat
        that as carrying phantom vig, never as a fair price.
        """
        raw = self.implied_probability()
        if self.counter_price is None:
            return raw, {"devigged": False, "method": "none",
                         "note": "no counter-quote; raw implied carries the vig"}
        counter = MarketQuote(price=self.counter_price, kind=self.kind)
        result = devig_market(
            [1.0 / raw, 1.0 / counter.implied_probability()]
        )
        # Overround sanity gate: a real two-way book carries positive but
        # bounded hold. overround <= 0 means the two sides cannot belong to
        # one live book (crossed/stale mix — a free lunch); > 0.5 means no
        # real book holds 50 points. Either way there is no honest fair
        # price to devig to, so refuse rather than manufacture one.
        if result["overround"] <= MIN_OVERROUND or result["overround"] > MAX_OVERROUND:
            return raw, {
                "devigged": False,
                "method": "refused",
                "overround": result["overround"],
                "error": (
                    f"book rejected: overround {result['overround']} outside "
                    f"({MIN_OVERROUND}, {MAX_OVERROUND}] — sides are crossed, "
                    f"stale-mixed or not one market"
                ),
                "raw_implied": raw,
            }
        fair = result["fair_probabilities"][0]
        return fair, {
            "devigged": True,
            "method": result["method"],
            "overround": result["overround"],
            "raw_implied": raw,
        }


# Overround sanity bounds (M1c). A real two-way book carries positive but
# bounded hold: <= 0 means the sides cannot both be live asks of one market
# (crossed/stale mix — an apparent free lunch); > MAX_OVERROUND means no
# real book holds that much. Books outside the window are refused, never
# devigged into a "fair" price.
MIN_OVERROUND = 0.0
MAX_OVERROUND = 0.60


def _to_decimal(price: Quote) -> float:
    p = _raw_implied(price)
    if p <= 0 or p >= 1:
        raise ValueError(f"implied probability {p} out of (0,1)")
    return 1.0 / p


# ---------------------------------------------------------------------------
# Edge assessment — the lifecycle stage itself
# ---------------------------------------------------------------------------

# Kelly fraction caps. Full Kelly on an estimated probability is aggressive;
# these bound the OUTPUT regardless of inputs. Automated actors may tighten,
# never loosen.
MAX_FRACTION_FULL_KELLY = 0.25
MIN_EDGE_TO_ACT = 0.005          # half a point of probability


@dataclass
class EdgeAssessment:
    """Everything a sealed conclusion needs before it becomes a position."""

    claim_id: str
    calibrated_prob: float                 # the research layer's honest belief
    quote: MarketQuote                     # market price AT CLAIM TIME (CLV anchor)
    market_prob_raw: float                 # raw implied
    market_prob_fair: float                # devigged
    devig_audit: dict = field(default_factory=dict)
    edge: float = 0.0                      # calibrated - fair (in probability points)
    kelly_fraction_full: float = 0.0       # full Kelly fraction of bankroll
    kelly_fraction_quarter: float = 0.0    # quarter-Kelly default
    ev_per_unit: float = 0.0               # expected value staking 1 unit at the offered price
    actionable: bool = False
    notes: list[str] = field(default_factory=list)

    def summary(self) -> dict:
        def _round_never_up(x: float, nd: int = 6) -> float:
            """Reporting quantiser: may only round DOWN in magnitude.

            Family 6 — round() half-up can move a reported edge UP by up
            to 5e-7, and an automated actor reading its own report must
            never see a bigger edge than was computed. Ties and truncation
            both go toward zero (the information-losing direction).
            """
            if x == 0.0:
                return 0.0
            return math.trunc(x * 10.0 ** nd) / 10.0 ** nd

        return {
            "claim_id": self.claim_id,
            "calibrated_prob": _round_never_up(self.calibrated_prob),
            "market_prob_raw": _round_never_up(self.market_prob_raw),
            "market_prob_fair": _round_never_up(self.market_prob_fair),
            "edge": _round_never_up(self.edge),
            "kelly_full": _round_never_up(self.kelly_fraction_full),
            "kelly_quarter": _round_never_up(self.kelly_fraction_quarter),
            "ev_per_unit": _round_never_up(self.ev_per_unit),
            "actionable": self.actionable,
            "quote": {
                "source": self.quote.source,
                "as_of": self.quote.as_of,
                "price": self.quote.price,
                "counter_price": self.quote.counter_price,
                **self.devig_audit,
            },
            "notes": self.notes,
        }


def assess_edge(
    claim_id: str,
    calibrated_prob: float,
    quote: MarketQuote,
    *,
    min_edge: float = MIN_EDGE_TO_ACT,
) -> EdgeAssessment:
    """Full pipeline: calibrated probability -> market probability -> edge
    -> Kelly -> assessment, with the price-at-claim-time carried through.

    Domain-general by construction: nothing here knows whether the quote came
    from a sportsbook, a prediction market, or an option chain.
    """
    if not 0.0 < calibrated_prob < 1.0:
        raise ValueError("calibrated_prob must be in (0, 1)")

    market_fair, audit = quote.fair_probability()
    market_raw = quote.implied_probability()

    edge = calibrated_prob - market_fair
    notes = []
    if not audit.get("devigged"):
        notes.append(
            "single-sided quote: market probability is RAW IMPLIED, not "
            "devigged — edge may include up to the full vig as phantom"
        )
    if audit.get("method") == "refused":
        notes.append(audit.get("error", "book rejected by overround gate"))

    # Decimal payout available at the quoted price.
    decimal = _to_decimal(quote.price)
    b = decimal - 1.0

    # ONE price, one copy (Family 2): edge is measured against the DEVIGGED
    # market probability, so Kelly and EV must be computed with the same
    # p. Sizing at the raw implied payout while claiming a devigged edge
    # lets an assessment report positive Kelly on a NEGATIVE edge — the
    # raw implied hides the vig that the edge definition already charged.
    # When no honest devig exists (single-sided or refused book) the only
    # safe p is the CONSERVATIVE one: the raw implied, which zeroes both.
    if audit.get("devigged"):
        p_win = calibrated_prob
        if edge < 0:
            notes.append(
                "negative edge vs devigged market: Kelly and EV zeroed "
                "(sizing is never computed against the raw implied)"
            )
    else:
        p_win = market_raw
        if calibrated_prob <= market_raw:
            notes.append(
                "no devig available: sizing gated to raw implied; "
                "calibrated prob does not beat it"
            )
    q = 1.0 - p_win
    kelly_full_frac = max(0.0, (b * p_win - q) / b)
    ev_per_unit = p_win * b - q      # stake 1, win b*p - q expectation
    if audit.get("method") == "refused":
        # A refused (crossed/stale-mixed) book has no honest price to size
        # against: sizing is zeroed outright, not recomputed on the raw side.
        if kelly_full_frac > 0 or ev_per_unit > 0:
            notes.append("refused book: Kelly and EV zeroed")
        kelly_full_frac = 0.0
        ev_per_unit = 0.0
    kelly_full_capped = min(kelly_full_frac, MAX_FRACTION_FULL_KELLY)
    if not audit.get("devigged") and ev_per_unit > 0:
        notes.append(
            "EV computed against RAW implied (no counter-quote): treat as "
            "upper bound carrying up to the full vig"
        )

    actionable = (
        edge >= min_edge
        and ev_per_unit > 0
        and kelly_full_frac > 0
        and audit.get("devigged", False)
    )
    if not audit.get("devigged") and actionable:
        actionable = False  # belt and braces: un-devigged books never act
    if kelly_full_frac > MAX_FRACTION_FULL_KELLY:
        notes.append(
            f"full Kelly {kelly_full_frac:.4f} capped at "
            f"{MAX_FRACTION_FULL_KELLY}"
        )

    return EdgeAssessment(
        claim_id=claim_id,
        calibrated_prob=calibrated_prob,
        quote=quote,
        market_prob_raw=market_raw,
        market_prob_fair=market_fair,
        devig_audit=audit,
        edge=edge,
        kelly_fraction_full=kelly_full_capped,
        kelly_fraction_quarter=kelly_full_capped / 4.0,
        ev_per_unit=ev_per_unit,
        actionable=actionable,
        notes=notes,
    )


# ---------------------------------------------------------------------------
# Generalised CLV: grade a claim-time price against a later price
# ---------------------------------------------------------------------------

def clv_points(claim_quote: MarketQuote, close_quote: MarketQuote) -> Optional[float]:
    """Closing-line value in probability POINTS, devigged both sides.

    Positive means the market moved TOWARD your claim after you took the
    price — the classic signal you bet the right side regardless of outcome.
    Requires counter quotes on both sides so both prices are genuinely
    devigged; comparing raw-to-raw is exactly the phantom-edge bug this is
    built to avoid. Returns None when devigging is impossible.
    """
    f_claim, a_claim = claim_quote.fair_probability()
    f_close, a_close = close_quote.fair_probability()
    # Both sides must be genuinely devigged AND from books that passed the
    # overround gate. A crossed/stale-mixed book is "devigged" by a naive
    # check; method == 'refused' marks one that was rejected. Scoring CLV
    # against an invalid book feeds phantom track-record data downstream.
    for side, audit in (("claim", a_claim), ("close", a_close)):
        if not audit.get("devigged") or audit.get("method") in ("refused", None) \
                or audit.get("error"):
            return None
    return f_close - f_claim


def clv_basis_points(claim_quote: MarketQuote, close_quote: MarketQuote) -> Optional[float]:
    v = clv_points(claim_quote, close_quote)
    return None if v is None else round(v * 10_000.0, 2)
