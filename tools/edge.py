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
    """Raw implied probability from any accepted price representation."""
    if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price):
        raise ValueError(f"price must be a finite number, got {price!r}")

    if isinstance(price, int) and price != 0 and abs(price) >= 100:
        # American odds are integers with |value| >= 100 by convention.
        return _american_to_implied(price)
    return _continuous_to_prob(float(price))


def _american_to_implied(american: int) -> float:
    if american > 0:
        return 100.0 / (american + 100.0)
    if american < 0:
        return (-american) / ((-american) + 100.0)
    raise ValueError("American odds of 0 are not a valid price")


def _continuous_to_prob(p: float) -> float:
    """Continuous representations: implied prob in [0,1], decimal odds > 1,
    or a contract price quoted in cents (e.g. 47 for $0.47).

    CONVENTION: an integral value in [2, 100) under kind='auto' is read as a
    CENT-QUOTED CONTRACT (Kalshi/Polymarket style), never as decimal odds.
    Decimal odds are conventionally quoted with decimals (1.91, 2.40); an
    exact integer in this range read as decimal odds would imply a 1-50%
    probability from what is almost certainly a 2-99 cent contract price
    (a ~22x error on a 47-cent quote). Callers with genuinely integral
    decimal odds must pass kind='decimal' explicitly."""
    if 0.0 < p <= 1.0:
        return p                      # already a probability
    if p > 1.0:
        if float(p).is_integer() and 2.0 <= p < 100.0:
            return p / 100.0          # cent-quoted contract
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
        try:
            counter = MarketQuote(price=self.counter_price, kind=self.kind)
            counter_implied = counter.implied_probability()
        except ValueError as e:
            return raw, {"devigged": False, "method": "none", "invalid_book": str(e),
                         "note": "counter-quote unparseable; raw implied carries the vig"}
        if not (math.isfinite(raw) and 0.0 < raw < 1.0
                and math.isfinite(counter_implied) and 0.0 < counter_implied < 1.0):
            return raw, {"devigged": False, "method": "none",
                         "invalid_book": "implied probabilities out of (0, 1)",
                         "note": "malformed two-sided quote; not devigged"}
        result = devig_market([1.0 / raw, 1.0 / counter_implied])
        if "error" in result:
            # Crossed / stale / absurd book: never manufacture a fair price.
            return raw, {
                "devigged": False,
                "invalid_book": result["error"],
                "overround": result.get("overround"),
                "raw_implied": raw,
                "note": "two-sided book failed the market-sanity gate; "
                        "raw implied returned but NOT fit for sizing",
            }
        fair = result["fair_probabilities"][0]
        return fair, {
            "devigged": True,
            "method": result["method"],
            "overround": result["overround"],
            "raw_implied": raw,
        }


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


def _floor6(x: float) -> float:
    """Round DOWN to 6 dp. Reporting must never nudge a stake or edge upward."""
    return math.floor(x * 1_000_000.0) / 1_000_000.0


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
        return {
            "claim_id": self.claim_id,
            "calibrated_prob": round(self.calibrated_prob, 6),
            "market_prob_raw": round(self.market_prob_raw, 6),
            "market_prob_fair": (
                round(self.market_prob_fair, 6)
                if math.isfinite(self.market_prob_fair) else self.market_prob_fair
            ),
            "edge": _floor6(self.edge) if math.isfinite(self.edge) else self.edge,
            "kelly_full": _floor6(self.kelly_fraction_full)
                if math.isfinite(self.kelly_fraction_full) else self.kelly_fraction_full,
            "kelly_quarter": _floor6(self.kelly_fraction_quarter)
                if math.isfinite(self.kelly_fraction_quarter) else self.kelly_fraction_quarter,
            "ev_per_unit": round(self.ev_per_unit, 6)
                if math.isfinite(self.ev_per_unit) else self.ev_per_unit,
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

    # Fail safe on invalid books: an unsanitary two-sided book must not yield
    # an actionable assessment or positive Kelly stake. Keep the existing
    # public shape — return an EdgeAssessment flagged invalid and inert.
    if (audit.get("invalid_book")
            or not math.isfinite(market_fair)
            or not 0.0 < market_fair < 1.0):
        return EdgeAssessment(
            claim_id=claim_id,
            calibrated_prob=calibrated_prob,
            quote=quote,
            market_prob_raw=market_raw,
            market_prob_fair=float("nan"),
            devig_audit=audit,
            edge=float("nan"),
            kelly_fraction_full=0.0,
            kelly_fraction_quarter=0.0,
            ev_per_unit=float("nan"),
            actionable=False,
            notes=["invalid market book: no fair probability; sizing refused"]
                  + [audit[k] for k in ("invalid_book",) if k in audit],
        )

    edge = calibrated_prob - market_fair
    notes = []
    if not audit.get("devigged"):
        notes.append(
            "single-sided quote: market probability is RAW IMPLIED, not "
            "devigged — edge may include up to the full vig as phantom"
        )

    # Decimal payout available at the quoted price.
    decimal = _to_decimal(quote.price)
    b = decimal - 1.0
    q = 1.0 - calibrated_prob
    # Kelly is only meaningful for a claim that beats the DEVIGGED market
    # price. A negative-fair-edge assessment must never carry a positive
    # stake, even if the raw payout arithmetic alone looks profitable.
    if edge <= 0.0:
        kelly_full_frac = 0.0
    else:
        kelly_full_frac = max(0.0, (b * calibrated_prob - q) / b)
    kelly_full_capped = min(kelly_full_frac, MAX_FRACTION_FULL_KELLY)

    ev_per_unit = calibrated_prob * b - q      # stake 1, win b*p - q expectation

    actionable = edge >= min_edge and ev_per_unit > 0
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
    if not (a_claim.get("devigged") and a_close.get("devigged")):
        return None
    if a_claim.get("invalid_book") or a_close.get("invalid_book"):
        return None
    return f_close - f_claim


def clv_basis_points(claim_quote: MarketQuote, close_quote: MarketQuote) -> Optional[float]:
    v = clv_points(claim_quote, close_quote)
    return None if v is None else round(v * 10_000.0, 2)
