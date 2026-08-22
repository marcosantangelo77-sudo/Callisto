"""Kalshi domain plugin — the third domain (first non-sports market).

Registered like finance and compute via tools.domain_registry. Serves no
AGP Domain value (event contracts are not FINANCIAL); routes on keywords
so a question like "what's the market saying about the next CPI print"
picks up these tools.

Tools exposed to the session:
    kalshi_list_markets(series_ticker, status, limit)
        discover contracts: CPI, Fed, GDP, earnings, weather, elections.
    kalshi_get_market(ticker)
        one contract: implied probability, resolution criteria verbatim,
        settlement result if settled.
    kalshi_market_edge(ticker, calibrated_prob)
        wires the live price into tools.edge.MarketQuote and assess_edge:
        calibrated probability vs devigged implied -> measured edge +
        Kelly + CLV anchor. COMPUTES ONLY — never places anything.

The dispatcher runs blocking fetches in an executor; one adapter is
shared process-wide so the 0.5s self-limit holds under bursts.
"""

import asyncio
import logging
import re

from tools.domain_registry import DomainPlugin
from tools.sources.base import RestSource

logger = logging.getLogger("callisto.kalshi_plugin")

_client = None


def _get_client():
    global _client
    if _client is None:
        from agp.provenance import ProvenanceLedger
        from tools.domains.kalshi.market import SPEC, KalshiAdapter

        _client = KalshiAdapter(RestSource(SPEC, ledger=ProvenanceLedger()))
    return _client


LIST_MARKETS_TOOL = {
    "type": "function",
    "function": {
        "name": "kalshi_list_markets",
        "description": (
            "List Kalshi event contracts (CFTC-regulated prediction "
            "market): CPI prints, Fed decisions, GDP, earnings, weather, "
            "elections. Returns tickers, titles, YES bid/ask as implied "
            "probabilities, volume. Read-only public market data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "series_ticker": {"type": "string",
                                  "description": "e.g. KXCPI, KXFED, KXHIGHNY"},
                "status": {"type": "string",
                           "enum": ["open", "closed", "settled"],
                           "description": "default open"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
}

GET_MARKET_TOOL = {
    "type": "function",
    "function": {
        "name": "kalshi_get_market",
        "description": (
            "One Kalshi contract by ticker: full title, YES/NO book as "
            "probabilities, mid implied probability, the RESOLUTION "
            "CRITERIA verbatim from the contract rules, settlement result "
            "when settled."
        ),
        "parameters": {
            "type": "object",
            "properties": {"ticker": {"type": "string"}},
            "required": ["ticker"],
        },
    },
}

EDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "kalshi_market_edge",
        "description": (
            "Compare a calibrated probability against a Kalshi contract's "
            "market-implied price: returns devigged market probability, "
            "measured edge, Kelly fractions and EV per unit. ANALYSIS "
            "ONLY — this tool computes an edge assessment and places no "
            "trade, touches no account, and cannot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string"},
                "calibrated_prob": {
                    "type": "number",
                    "description": "the research layer's honest belief, 0-1"},
                "min_edge": {"type": "number",
                             "description": "actionability threshold in "
                                            "probability points (default 0.005)"},
            },
            "required": ["ticker", "calibrated_prob"],
        },
    },
}


def _list_payload(args: dict) -> dict:
    page = _get_client().list_markets(
        series_ticker=str(args.get("series_ticker") or ""),
        status=str(args.get("status") or ""),
        limit=int(args.get("limit", 50)),
    )
    out = []
    for m in page["markets"]:
        out.append({
            "ticker": m.ticker,
            "title": m.title,
            "status": m.status,
            "result": m.resolved_outcome(),
            "yes_bid": m.yes_bid,
            "yes_ask": m.yes_ask,
            "implied_prob_mid": m.mid,
            "close_time": m.close_time,
            "volume": m.volume,
        })
    return {"markets": out, "cursor": page["cursor"], "_fetch": page["_fetch"]}


def _market_payload(ticker: str) -> dict:
    m = _get_client().get_market(str(ticker))
    return {
        "ticker": m.ticker,
        "title": m.title,
        "status": m.status,
        "result": m.resolved_outcome(),
        "yes_bid": m.yes_bid,
        "yes_ask": m.yes_ask,
        "no_bid": m.no_bid,
        "no_ask": m.no_ask,
        "implied_prob_mid": m.mid,
        "resolution_criteria": m.rules_primary,
        "close_time": m.close_time,
        "volume": m.volume,
        "open_interest": m.open_interest,
        "_fetch": m.raw.get("_fetch"),
    }


def _edge_payload(ticker: str, calibrated_prob: float,
                  min_edge: float) -> dict:
    from tools.domains.kalshi.market import kalshi_edge_assessment

    client = _get_client()
    quote, meta = client.market_quote(str(ticker))
    a = kalshi_edge_assessment(
        str(calibrated_prob), quote, claim_id=f"kalshi:{ticker}",
        min_edge=min_edge)
    d = a.summary()
    d["market"] = meta
    # The mandate line every consumer of this output must read.
    d["disposition"] = (
        "measurement only — Callisto reads public prices and computes; "
        "no trade was or can be placed through this system")
    return d


async def _execute(name: str, arguments: dict) -> dict:
    loop = asyncio.get_event_loop()

    def _run(name: str, args: dict) -> dict:
        if name == "kalshi_list_markets":
            return _list_payload(args)
        if name == "kalshi_get_market":
            return _market_payload(args.get("ticker", ""))
        if name == "kalshi_market_edge":
            cp = float(args.get("calibrated_prob"))
            if not 0.0 < cp < 1.0:
                raise ValueError("calibrated_prob must be in (0, 1)")
            return _edge_payload(args.get("ticker", ""), cp,
                                 float(args.get("min_edge", 0.005)))
        raise ValueError(f"kalshi plugin does not own tool {name!r}")

    try:
        result = await loop.run_in_executor(None, _run, name, dict(arguments))
        result["ok"] = True
        return result
    except Exception as exc:  # surfaced to the model as tool error text
        logger.warning("kalshi tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "tool": name}


_KEYWORDS = re.compile(
    r"\b(kalshi|prediction market|event contract|implied probabilit\w+|"
    r"cpi print|fed decision|rate decision|fomc odds|market odds|"
    r"will the (fed|cpi|fomc)|chances? of (a )?(rate|recession|shutdown))\b",
    re.IGNORECASE,
)


def build_kalshi_plugin() -> DomainPlugin:
    return DomainPlugin(
        name="kalshi",
        domains=set(),   # event contracts are not an AGP Domain value
        keywords=_KEYWORDS,
        tool_schemas=[LIST_MARKETS_TOOL, GET_MARKET_TOOL, EDGE_TOOL],
        freshness=[],    # prices are point-in-time by nature
        execute=_execute,
    )


def register_if_available(registry) -> bool:
    """Register iff the kalshi modules import cleanly (mirrors finance)."""
    try:
        import tools.domains.kalshi.market  # noqa: F401
    except ImportError:
        logger.info("kalshi plugin unavailable (modules not merged yet)")
        return False
    if "kalshi" not in {p.name for p in registry.plugins()}:
        registry.register(build_kalshi_plugin())
    return True
