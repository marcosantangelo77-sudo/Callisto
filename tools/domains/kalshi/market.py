"""Kalshi market data adapter — the second prediction-market domain.

CFTC-regulated exchange (tier 3: market prices). The public Trade API v2
market-data endpoints need NO authentication; this module touches ONLY
those. There is deliberately no order, portfolio, or account path anywhere
in this package — Callisto reads public prices and computes; it never
places, executes, or authorises a trade.

Base URL is configurable via CALLISTO_KALSHI_BASE_URL for tests and for
surviving Kalshi's documented host migrations (external-api.kalshi.com,
api.elections.kalshi.com — both serve all markets, not just elections).

Prices arrive as fixed-decimal dollar STRINGS ("0.6300"); parse them once
here so every downstream consumer works in probability floats. A YES price
in [0,1] IS the implied probability — MarketQuote(kind="contract_cents")
expects cents, so this adapter emits kind="probability" quotes instead.
Two-sided yes_bid/yes_ask gives a genuine devig without touching the NO
book, but the no_bid side is carried too because it is the tighter
counterparty when spreads are wide.

Answers: event-implied probabilities across CPI prints, Fed decisions,
GDP, earnings, weather, elections; resolution criteria verbatim from the
contract rules; settlement results after close.
Cannot answer: order-book depth beyond top-of-book (needs /orderbook),
trade-level history (GET /markets/trades exists but we do not consume it),
anything requiring credentials (orders, positions, balance) — by design.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any, Optional

from tools.sources.base import RestSource, SourceError, SourceSpec

DEFAULT_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

SPEC = SourceSpec(
    name="kalshi",
    base_url=os.environ.get("CALLISTO_KALSHI_BASE_URL", DEFAULT_BASE_URL),
    description=(
        "Kalshi CFTC-regulated event contracts: market-implied probabilities "
        "for CPI, Fed decisions, GDP, earnings, weather, elections"
    ),
    answers=(
        "market-implied probability of an economic or world event",
        "event contract prices for CPI inflation, Fed rate decisions, GDP, "
        "earnings beats, weather, elections",
        "resolution criteria and settlement result of a binary event contract",
    ),
    cannot_answer=(
        "order-book depth or trade history (only top-of-book quotes consumed)",
        "any authenticated operation: placing trades, viewing positions "
        "or balances — this adapter is public market-data read-only by mandate",
        "probabilities for events with no listed contract",
    ),
    tier=3,
    min_interval_s=0.5,
    terms_url="https://kalshi.com/docs/kalshi-api-terms-of-use.pdf",
)

_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9\-\.]*$")


def _parse_price(raw: Any) -> Optional[float]:
    """Parse Kalshi's fixed-decimal dollar string ("0.6300") to float."""
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
class KalshiMarket:
    """One binary event contract, normalised onto probabilities."""

    ticker: str
    event_ticker: str
    title: str
    status: str                       # active | settled | closed | ...
    result: str                       # "" until settled, then "yes"/"no"
    yes_bid: Optional[float] = None   # dollars 0..1 == probability
    yes_ask: Optional[float] = None
    no_bid: Optional[float] = None
    no_ask: Optional[float] = None
    last_price: Optional[float] = None
    rules_primary: str = ""           # the RESOLUTION CRITERIA, verbatim
    close_time: str = ""
    expected_expiration_time: str = ""
    volume: Optional[float] = None
    open_interest: Optional[float] = None
    raw: dict = field(default_factory=dict)   # untouched payload, provenance

    @classmethod
    def from_api(cls, d: dict) -> "KalshiMarket":
        ticker = d.get("ticker", "")
        if not _TICKER_RE.match(ticker):
            raise ValueError(f"malformed Kalshi market ticker {ticker!r}")
        return cls(
            ticker=ticker,
            event_ticker=d.get("event_ticker", ""),
            title=d.get("title", ""),
            status=d.get("status", ""),
            result=(d.get("result") or "").strip().lower(),
            yes_bid=_parse_price(d.get("yes_bid_dollars")),
            yes_ask=_parse_price(d.get("yes_ask_dollars")),
            no_bid=_parse_price(d.get("no_bid_dollars")),
            no_ask=_parse_price(d.get("no_ask_dollars")),
            last_price=_parse_price(d.get("last_price_dollars")),
            rules_primary=d.get("rules_primary", ""),
            close_time=d.get("close_time", ""),
            expected_expiration_time=d.get("expected_expiration_time", ""),
            volume=_parse_float(d.get("volume_fp")),
            open_interest=_parse_float(d.get("open_interest_fp")),
            raw=dict(d),
        )

    # ── derived views ────────────────────────────────────────────────────

    @property
    def mid(self) -> Optional[float]:
        """Midpoint of the YES book — the headline implied probability."""
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return round((self.yes_bid + self.yes_ask) / 2.0, 4)

    @property
    def is_settled(self) -> bool:
        return self.status == "settled" and self.result in ("yes", "no")

    def resolved_outcome(self) -> Optional[str]:
        """'yes' | 'no' | None (unsettled or indeterminate)."""
        if self.result in ("yes", "no"):
            return self.result
        return None


def _parse_float(raw: Any) -> Optional[float]:
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


class KalshiAdapter:
    """Read-only client over the unauthenticated market-data endpoints."""

    def __init__(self, source: RestSource):
        self.source = source

    # ── listing ──────────────────────────────────────────────────────────

    def list_markets(self, *, series_ticker: str = "", event_ticker: str = "",
                     status: str = "", limit: int = 100,
                     cursor: str = "") -> dict:
        """Page of markets. Returns {'markets': [KalshiMarket], 'cursor': str,
        '_fetch': provenance}. Exactly one of series/event/status filters;
        empty returns everything."""
        params: dict[str, Any] = {"limit": max(1, min(int(limit), 1000))}
        filters = {
            "series_ticker": series_ticker,
            "event_ticker": event_ticker,
            "status": status,
            "cursor": cursor,
        }
        set_filters = {k: v for k, v in filters.items() if v}
        if len([k for k in set_filters if k in ("series_ticker", "event_ticker")]) > 1:
            raise ValueError("filter by series_ticker OR event_ticker, not both")
        params.update(set_filters)
        url = self.source.build_url("/markets", params)
        data, rec = self.source.get_json(url)
        markets = [KalshiMarket.from_api(m) for m in data.get("markets", [])]
        return {"markets": markets,
                "cursor": data.get("cursor", ""),
                "_fetch": _fetch_meta(rec)}

    def get_market(self, ticker: str) -> KalshiMarket:
        """One market by full ticker (e.g. 'KXHIGHNY-26AUG23-T87')."""
        ticker = str(ticker).strip()
        if not _TICKER_RE.match(ticker):
            raise ValueError(f"malformed Kalshi market ticker {ticker!r}")
        url = self.source.build_url(f"/markets/{ticker}")
        data, rec = self.source.get_json(url)
        m = data.get("market")
        if not m:
            raise SourceError(f"kalshi returned no market for {ticker}")
        m["_fetch"] = _fetch_meta(rec)
        return KalshiMarket.from_api(m)

    def iter_markets(self, *, series_ticker: str = "", status: str = "",
                     limit: int = 100, max_pages: int = 10):
        """Cursor-paginated listing generator (rate-limited by RestSource)."""
        cursor = ""
        pages = 0
        while pages < max_pages:
            page = self.list_markets(series_ticker=series_ticker,
                                     status=status, limit=limit, cursor=cursor)
            yield from page["markets"]
            cursor = page["cursor"]
            pages += 1
            if not cursor:
                return

    # ── edge wiring (tools/edge.py) ──────────────────────────────────────

    def market_quote(self, ticker: str):
        """The market's live price as a tools.edge.MarketQuote.

        YES mid as `price`, YES bid as `counter_price`, kind='probability'
        (Kalshi dollars ARE probabilities). With both sides present the
        quote devigs via the standard two-way path, so assess_edge() sees a
        fair market probability rather than raw implied. Returns
        (quote, fetch_meta); raises ValueError on a stale/unpriced book.
        """
        import time as _time

        from tools.edge import MarketQuote

        m = self.get_market(ticker)
        if m.yes_ask is None or m.no_ask is None:
            raise ValueError(
                f"market {m.ticker} has no two-sided book "
                f"(yes_ask={m.yes_ask} no_ask={m.no_ask})")
        # Complementary OFFERS devig honestly: buying YES at its ask is the
        # exact complement of buying NO at its ask, and their sum carries
        # the spread as measurable overround.
        quote = MarketQuote(
            price=m.yes_ask,
            counter_price=m.no_ask,
            kind="probability",
            source="kalshi",
            as_of=_time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        )
        return quote, {"url": f"{self.source.spec.base_url}/markets/{m.ticker}",
                       "ticker": m.ticker, "title": m.title,
                       "yes_bid": m.yes_bid, "yes_ask": m.yes_ask,
                       "no_ask": m.no_ask}

    # ── outcome scoring (tools/resolvers/base.py shape) ──────────────────

    def resolution(self, ticker: str) -> dict:
        """Resolution criteria + settlement result for one contract.

        This is what makes a Kalshi contract a *resolved claim*: the rules
        text is fetched at claim time and again at settlement, and the
        result field carries ground truth straight from the exchange.
        """
        m = self.get_market(ticker)
        return {
            "ticker": m.ticker,
            "title": m.title,
            "criteria": m.rules_primary,
            "status": m.status,
            "result": m.resolved_outcome(),
            "close_time": m.close_time,
            "_fetch": m.raw.get("_fetch"),
        }


def _fetch_meta(rec) -> dict:
    return {"url": rec.url, "sha256": rec.content_sha256,
            "fetched_at": rec.fetched_at}


# ---------------------------------------------------------------------------
# Edge + CLV wiring (tools/edge.py)
# ---------------------------------------------------------------------------

def kalshi_edge_assessment(calibrated_prob: float, quote, *,
                           claim_id: str = "kalshi",
                           min_edge: float = 0.005):
    """assess_edge over a Kalshi quote — thin so the math stays in edge.py.

    calibrated probability vs the contract's devigged implied probability
    becomes a measured edge, Kelly fraction and EV per unit. Measurement
    only; nothing here can place or authorise an order.
    """
    from tools.edge import assess_edge

    return assess_edge(claim_id, float(calibrated_prob), quote,
                       min_edge=min_edge)


def kalshi_clv_basis_points(claim_quote, close_quote) -> Optional[float]:
    """Devigged CLV in basis points between claim-time and close prices.

    The generalised-CLV loop for event contracts: did the market move
    toward the claim after we measured it? Both quotes need two-sided
    books or this returns None (refuses raw-to-raw comparison).
    """
    from tools.edge import clv_basis_points

    return clv_basis_points(claim_quote, close_quote)
