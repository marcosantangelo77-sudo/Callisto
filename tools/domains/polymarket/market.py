"""Polymarket market-data adapter — the third prediction-market domain.

Polymarket is an unregulated (non-CFTC) on-chain prediction market: a
different venue, different participants, different contract set, and —
critically — a DIFFERENT INDEPENDENCE FAMILY QUESTION from Kalshi. Where
both price the same event, see tools/sources/base.py INDEPENDENCE_FAMILIES
and findings/polymarket_independence.md: we do NOT assume the two prices
are independent, because that would inflate confidence.

Two public APIs, both unauthenticated, both read-only:

  Gamma API   https://gamma-api.polymarket.com   market metadata: question,
              description (the RESOLUTION CRITERIA verbatim), outcome
              prices, resolution status, volume, end date.
  CLOB API    https://clob.polymarket.com        top-of-book: /book and
              /price per ERC-1155 token id. The YES token's buy side is
              its BID and its sell side is its ASK; the NO side is the
              exact complement (no_ask == 1 - yes_bid) because NO shares
              are minted from collateral, not quoted separately.

Prices arrive as probability floats in [0,1] (a Polymarket share pays $1
on YES). A YES ask IS the raw implied probability — MarketQuote(
kind="probability"). Like the Kalshi adapter we emit the two complementary
OFFERS into MarketQuote, never the mid: you cannot transact at the mid,
and yes_ask + no_ask carries the spread as measurable overround that the
two-way devig removes.

READ-ONLY BY MANDATE: this module touches only public market-data GETs.
There is no wallet, no key, no order path, no account access anywhere in
this package — Callisto reads public prices and computes; it never places,
executes, or authorises a trade.

CALLISTO_POLYMARKET_GAMMA_URL / CALLISTO_POLYMARKET_CLOB_URL override the
base URLs for tests.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from tools.sources.base import RestSource, SourceError, SourceSpec

DEFAULT_GAMMA_URL = "https://gamma-api.polymarket.com"
DEFAULT_CLOB_URL = "https://clob.polymarket.com"

SPEC = SourceSpec(
    name="polymarket",
    base_url=os.environ.get("CALLISTO_POLYMARKET_GAMMA_URL", DEFAULT_GAMMA_URL),
    description=(
        "Polymarket prediction market: market-implied probabilities for "
        "crypto, geopolitics, culture, science milestones, elections"
    ),
    answers=(
        "market-implied probability of a political, crypto, cultural, or "
        "science event",
        "prediction-market odds for events Kalshi does not list",
        "resolution criteria and settlement result of a Polymarket contract",
    ),
    cannot_answer=(
        "order-book depth beyond the top of book consumed here",
        "any authenticated operation: placing trades, wallets, balances, "
        "allowances — this adapter is public market-data read-only by mandate",
        "probabilities for events with no listed contract",
    ),
    tier=3,
    min_interval_s=0.5,
    terms_url="https://polymarket.com/terms",
)

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{0,199}$")
_TOKEN_ID_RE = re.compile(r"^\d{1,90}$")


def _parse_json_str(raw: Any) -> Any:
    """Gamma serialises several array fields as JSON *strings*."""
    if isinstance(raw, (list, tuple)):
        return list(raw)
    if isinstance(raw, str):
        try:
            v = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return v if isinstance(v, list) else None
    return None


def _parse_price(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not 0.0 <= v <= 1.0:
        return None
    return v


@dataclass(frozen=True)
class PolyMarket:
    """One binary Polymarket contract, normalised onto probabilities."""

    id: str
    question: str
    slug: str
    condition_id: str
    description: str                  # the RESOLUTION CRITERIA, verbatim
    closed: bool = False
    active: bool = False
    uma_resolution_status: str = ""   # "" while open, "resolved" when settled
    outcome_prices: tuple = ()        # [yes_price, no_price] at last trade/mark
    end_date_iso: str = ""
    volume_num: Optional[float] = None
    liquidity_num: Optional[float] = None
    yes_token_id: str = ""
    no_token_id: str = ""
    event_slug: str = ""
    raw: dict = field(default_factory=dict)   # untouched payload, provenance

    @classmethod
    def from_api(cls, d: dict) -> "PolyMarket":
        mid = str(d.get("id") or "")
        if not mid.isdigit():
            raise ValueError(f"malformed Polymarket market id {mid!r}")
        tokens = _parse_json_str(d.get("clobTokenIds")) or []
        outcomes = [str(o).strip().lower()
                    for o in (_parse_json_str(d.get("outcomes")) or [])]
        prices = _parse_json_str(d.get("outcomePrices")) or []
        yes_tok = no_tok = ""
        if len(tokens) >= 2:
            if outcomes and outcomes[0] == "no":
                no_tok, yes_tok = str(tokens[0]), str(tokens[1])
            else:
                yes_tok, no_tok = str(tokens[0]), str(tokens[1])
        evts = d.get("events") or []
        return cls(
            id=mid,
            question=d.get("question", ""),
            slug=d.get("slug", ""),
            condition_id=d.get("conditionId", ""),
            description=d.get("description", ""),
            closed=bool(d.get("closed")),
            active=bool(d.get("active")),
            uma_resolution_status=(d.get("umaResolutionStatus") or "").strip().lower(),
            outcome_prices=tuple(_parse_price(p) for p in prices[:2]),
            end_date_iso=d.get("endDateIso") or d.get("endDate", ""),
            volume_num=_parse_float(d.get("volumeNum")),
            liquidity_num=_parse_float(d.get("liquidityNum")),
            yes_token_id=yes_tok,
            no_token_id=no_tok,
            event_slug=(evts[0].get("slug", "") if evts else ""),
            raw=dict(d),
        )

    # ── derived views ────────────────────────────────────────────────────

    @property
    def is_settled(self) -> bool:
        """Settled = UMA finalised AND the outcome actually decided."""
        return self.closed and self.uma_resolution_status == "resolved" \
            and self.resolved_outcome() is not None

    def resolved_outcome(self) -> Optional[str]:
        """'yes' | 'no' | None (unsettled or indeterminate).

        outcome_prices are strings on the wire ("1" / "0"); after parsing,
        a settled market shows ~1.0 on the winning side.
        """
        if len(self.outcome_prices) < 2:
            return None
        y, n = self.outcome_prices[0], self.outcome_prices[1]
        if y is None or n is None:
            return None
        if y >= 0.999 and n <= 0.001:
            return "yes"
        if n >= 0.999 and y <= 0.001:
            return "no"
        return None


def _parse_float(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class PolymarketAdapter:
    """Read-only client over Polymarket's unauthenticated public APIs."""

    def __init__(self, source: RestSource, *,
                 clob_url: str = ""):
        self.source = source
        self.clob_url = clob_url or os.environ.get(
            "CALLISTO_POLYMARKET_CLOB_URL", DEFAULT_CLOB_URL)

    # ── listing ──────────────────────────────────────────────────────────

    def list_markets(self, *, closed: bool = False, limit: int = 100,
                     offset: int = 0, order: str = "volumeNum") -> dict:
        """Page of markets ordered by volume. Returns {'markets':
        [PolyMarket], '_fetch': provenance}."""
        params: dict[str, Any] = {
            "closed": "true" if closed else "false",
            "limit": max(1, min(int(limit), 500)),
            "offset": max(0, int(offset)),
            "order": order,
            "ascending": "false",
        }
        url = self.source.build_url("/markets", params)
        data, rec = self.source.get_json(url)
        markets = [PolyMarket.from_api(m) for m in data]
        return {"markets": markets, "_fetch": _fetch_meta(rec)}

    def get_market(self, ref: str) -> PolyMarket:
        """One market by numeric id or by slug."""
        ref = str(ref).strip()
        if _SLUG_RE.match(ref) and not ref.isdigit():
            path = f"/markets/slug/{ref}"
        elif ref.isdigit():
            path = f"/markets/{ref}"
        else:
            raise ValueError(f"malformed Polymarket market reference {ref!r}")
        url = self.source.build_url(path)
        data, rec = self.source.get_json(url)
        if not data or not data.get("id"):
            raise SourceError(f"polymarket returned no market for {ref}")
        m = PolyMarket.from_api(data)
        m.raw["_fetch"] = _fetch_meta(rec)
        return m

    # ── top-of-book ──────────────────────────────────────────────────────

    def get_book(self, token_id: str) -> dict:
        """Top-of-book for one ERC-1155 token: {'best_bid', 'best_ask'}."""
        token_id = str(token_id).strip()
        if not _TOKEN_ID_RE.match(token_id):
            raise ValueError(f"malformed Polymarket token id {token_id!r}")
        url = f"{self.clob_url.rstrip('/')}/book?" + \
            urllib.parse.urlencode({"token_id": token_id})
        data, rec = self.source.get_json(url)
        bids = [_parse_price(b.get("price")) for b in (data.get("bids") or [])]
        asks = [_parse_price(a.get("price")) for a in (data.get("asks") or [])]
        bids = [b for b in bids if b is not None]
        asks = [a for a in asks if a is not None]
        return {
            "best_bid": max(bids) if bids else None,
            "best_ask": min(asks) if asks else None,
            "_fetch": _fetch_meta(rec),
        }

    # ── edge wiring (tools/edge.py) ──────────────────────────────────────

    def market_quote(self, ref: str):
        """The market's live price as a tools.edge.MarketQuote.

        Complementary OFFERS, deliberately — never the mid. The YES ask
        comes off the YES token's sell side; the NO ask is the complement
        of the YES bid (NO shares are minted from collateral, so no_ask =
        1 - yes_bid exactly). yes_ask + no_ask therefore sums above 1.0 by
        exactly the half-spread-plus-tick, and that overround is what the
        standard two-way devig removes. You cannot transact at the mid;
        pricing an edge off it silently overstates the edge.

        Returns (quote, fetch_meta); raises ValueError on a stale/unpriced
        book.
        """
        m = self.get_market(ref)
        if not m.yes_token_id:
            raise ValueError(f"market {m.id} has no CLOB token ids")
        yes_book = self.get_book(m.yes_token_id)
        if yes_book["best_bid"] is None or yes_book["best_ask"] is None:
            raise ValueError(
                f"market {m.id} has no two-sided YES book "
                f"(bid={yes_book['best_bid']} ask={yes_book['best_ask']})")
        quote, _book_meta = self.quote_from_book(
            yes_book["best_bid"], yes_book["best_ask"], m=m)
        meta = {"url": yes_book["_fetch"]["url"], "id": m.id,
                "question": m.question, "slug": m.slug,
                "yes_bid": yes_book["best_bid"],
                "yes_ask": yes_book["best_ask"]}
        return quote, meta

    # Impossible prices are clamped inside this band so a garbage feed can
    # never reach the devig with a negative or zero-sum pair of offers.
    _PRICE_FLOOR = 0.001

    @staticmethod
    def quote_from_book(yes_bid: float, yes_ask: float, *, m=None):
        """MarketQuote from a two-sided YES book (pure; unit-testable).

        H1c/H4a (red team): a CROSSED book (bid > ask — data glitch or race)
        used to pass straight through: yes_ask + no_ask then sums BELOW 1.0,
        the devig sees negative overround, and reports a fair probability
        ABOVE the ask — free money manufactured out of a broken book. A
        crossed input is instead read as the only self-consistent wide book
        (sorted), impossible prices are clamped into (0,1), and the repair
        is flagged in the returned meta so callers can reject the feed.
        """
        from tools.edge import MarketQuote

        yb, ya = float(yes_bid), float(yes_ask)
        meta = {"yes_bid": yes_bid, "yes_ask": yes_ask}
        if yb > ya:
            yb, ya = ya, yb
            meta["crossed_input_repaired"] = True
        floor = PolymarketAdapter._PRICE_FLOOR
        if yb < floor or yb > 1 - floor or ya < floor or ya > 1 - floor:
            yb = min(max(yb, floor), 1 - floor)
            ya = min(max(ya, floor), 1 - floor)
            meta["prices_clamped"] = True
        no_ask = round(1.0 - yb, 6)
        meta["no_ask"] = no_ask
        return MarketQuote(
            price=ya,
            counter_price=no_ask,
            kind="probability",
            source="polymarket",
            as_of=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        ), meta

    # ── outcome scoring (tools/resolvers/base.py shape) ──────────────────

    def resolution(self, ref: str) -> dict:
        """Resolution criteria + settlement result for one contract.

        This is what makes a Polymarket contract a *resolved claim*: the
        description text is fetched at claim time and again at settlement,
        and the UMA status + outcome prices carry ground truth.
        """
        m = self.get_market(ref)
        return {
            "id": m.id,
            "question": m.question,
            "slug": m.slug,
            "criteria": m.description,
            "uma_resolution_status": m.uma_resolution_status,
            "outcome_prices": list(m.outcome_prices),
            "result": m.resolved_outcome(),
            "end_date": m.end_date_iso,
            "_fetch": m.raw.get("_fetch"),
        }


def _fetch_meta(rec) -> dict:
    return {"url": rec.url, "sha256": rec.content_sha256,
            "fetched_at": rec.fetched_at}


# ---------------------------------------------------------------------------
# Edge + CLV wiring (tools/edge.py)
# ---------------------------------------------------------------------------

def polymarket_edge_assessment(calibrated_prob: float, quote, *,
                               claim_id: str = "polymarket",
                               min_edge: float = 0.005):
    """assess_edge over a Polymarket quote — thin so the math stays in edge.py.

    calibrated probability vs the contract's devigged implied probability
    becomes a measured edge, Kelly fraction and EV per unit. Measurement
    only; nothing here can place or authorise an order.
    """
    from tools.edge import assess_edge

    return assess_edge(claim_id, float(calibrated_prob), quote,
                       min_edge=min_edge)


def polymarket_clv_basis_points(claim_quote, close_quote) -> Optional[float]:
    """Devigged CLV in basis points between claim-time and close prices.

    The generalised-CLV loop for event contracts: did the market move
    toward the claim after we measured it? Both quotes need two-sided
    books or this returns None (refuses raw-to-raw comparison).
    """
    from tools.edge import clv_basis_points

    return clv_basis_points(claim_quote, close_quote)
