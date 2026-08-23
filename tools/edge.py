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
from datetime import datetime, timezone
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
    if 2 <= price < 100 and float(price).is_integer():
        # H1a (red team): a WHOLE number in [2, 100) is a contract price
        # quoted in cents (Kalshi/Polymarket), not decimal odds of 47-to-1.
        # int 47 and float 47.0 are the same price and must parse the same;
        # previously both silently became decimal odds -> implied 2.1%.
        # Non-integral values in this band stay decimal odds (2.13 is
        # legitimately 2.13-to-1); callers with cent prices should still
        # prefer kind="contract_cents".
        return float(price) / 100.0
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

        H1e (red team): the counter is parsed INDEPENDENTLY (auto semantics),
        not under this quote's kind — a cent price pasted in as an American
        or decimal counter used to be silently reinterpreted and devigged
        against, inflating our side.
        """
        raw = self.implied_probability()
        if self.counter_price is None:
            return raw, {"devigged": False, "method": "none",
                         "note": "no counter-quote; raw implied carries the vig"}
        try:
            counter_implied = _raw_implied(self.counter_price)
        except ValueError as e:
            return raw, {"devigged": False, "method": "invalid_counter",
                         "note": f"counter-quote unparseable: {e}"}
        if not (0.0 < raw < 1.0) or not (0.0 < counter_implied < 1.0):
            # An implied probability outside (0,1) means the sides were not
            # both real prices; report the overround so the garbage is
            # visible, but refuse to call anything devigged.
            return raw, {
                "devigged": False,
                "method": "invalid_sides",
                "overround": round(raw + counter_implied - 1.0, 6),
                "raw_implied": raw,
                "note": (
                    f"side implied {raw:.4g} / counter implied "
                    f"{counter_implied:.4g} outside (0, 1) — mixed-format or "
                    f"impossible quote; refusing to devig"
                ),
            }
        result = devig_market(
            [1.0 / raw, 1.0 / counter_implied]
        )
        if "error" in result:
            # Sub-fair / crossed book: nothing to devig (tools/devig refused).
            return raw, {"devigged": False, "method": "refused",
                         "overround": result["overround"],
                         "note": result["error"]}
        fair = result["fair_probabilities"][0]
        return fair, {
            "devigged": True,
            "method": result["method"],
            "overround": result["overround"],
            "raw_implied": raw,
        }


def _american_to_decimal(american: int) -> float:
    if american > 0:
        return 1.0 + american / 100.0
    return 1.0 + 100.0 / abs(american)


def _quote_decimal(quote: "MarketQuote") -> float:
    """Decimal payout odds for OUR side, honouring the declared kind.

    H1e/H1g (red team): this used to re-parse the price with auto semantics
    regardless of kind, so a decimal-odds 3.0 read as 3 cents (and a
    probability-kind negative slipped through). The payout must come from
    the representation the caller declared.
    """
    kind = quote.kind
    price = quote.price
    if kind == "auto":
        p = _raw_implied(price)
        if not 0.0 < p < 1.0:
            raise ValueError(f"implied probability {p} out of (0,1)")
        return 1.0 / p
    if kind == "decimal":
        d = float(price)
    elif kind == "probability":
        d = 1.0 / float(price)
    elif kind == "contract_cents":
        d = 100.0 / float(price)
    elif kind == "american":
        d = _american_to_decimal(int(price))
    else:
        raise ValueError(f"unknown quote kind {kind!r}")
    if not math.isfinite(d) or d <= 1.0:
        raise ValueError(
            f"quote implies decimal odds {d}; payout must exceed 1.0 to risk against")
    return d


# ---------------------------------------------------------------------------
# Edge assessment — the lifecycle stage itself
# ---------------------------------------------------------------------------

# Kelly fraction caps. Full Kelly on an estimated probability is aggressive;
# these bound the OUTPUT regardless of inputs. Automated actors may tighten,
# never loosen.
MAX_FRACTION_FULL_KELLY = 0.25
MIN_EDGE_TO_ACT = 0.005          # half a point of probability

# H1d (red team): a quote older than this may not arm a position. A stale
# price silently sizes against a market that has long since moved. Empty or
# unparseable as_of counts as UNVERIFIABLE and is refused the same way —
# "no timestamp" is not fresh, it is unauditable. May be tightened; never
# loosened.
MAX_QUOTE_AGE_S = 24 * 3600.0


def _parse_as_of(as_of: str) -> Optional[datetime]:
    """Parse an ISO-8601 quote timestamp; naive values are read as UTC."""
    text = (as_of or "").strip()
    if not text:
        return None
    try:
        ts = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


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
            "market_prob_fair": round(self.market_prob_fair, 6),
            "edge": round(self.edge, 6),
            "kelly_full": round(self.kelly_fraction_full, 6),
            "kelly_quarter": round(self.kelly_fraction_quarter, 6),
            "ev_per_unit": round(self.ev_per_unit, 6),
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
            "single-sided or undeviggable quote: market probability is RAW "
            "IMPLIED, not devigged — edge may include up to the full vig as "
            "phantom, and it may not arm a position"
        )

    # Decimal payout available at the quoted price.
    decimal = _quote_decimal(quote)
    b = decimal - 1.0
    q = 1.0 - calibrated_prob
    kelly_full_frac = max(0.0, (b * calibrated_prob - q) / b)
    kelly_full_capped = min(kelly_full_frac, MAX_FRACTION_FULL_KELLY)

    ev_per_unit = calibrated_prob * b - q      # stake 1, win b*p - q expectation

    actionable = edge >= min_edge and ev_per_unit > 0 and audit.get("devigged", False)

    # H1d (red team): freshness gate. A stale quote — or one with no
    # verifiable timestamp at all — measures a market that no longer exists.
    ts = _parse_as_of(quote.as_of)
    if ts is None:
        actionable = False
        notes.append(
            "quote has no parseable as_of timestamp: freshness unverifiable, "
            "not actionable"
        )
    else:
        age_s = (datetime.now(timezone.utc) - ts).total_seconds()
        if age_s < -300.0:
            # timestamp in the future beyond clock-skew tolerance: bad feed
            actionable = False
            notes.append(f"quote as_of {quote.as_of} is in the future")
        elif age_s > MAX_QUOTE_AGE_S:
            actionable = False
            notes.append(
                f"quote is stale: as_of {quote.as_of} exceeds the "
                f"{MAX_QUOTE_AGE_S / 3600.0:.0f}h freshness gate"
            )

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
    return f_close - f_claim


def clv_basis_points(claim_quote: MarketQuote, close_quote: MarketQuote) -> Optional[float]:
    v = clv_points(claim_quote, close_quote)
    return None if v is None else round(v * 10_000.0, 2)
