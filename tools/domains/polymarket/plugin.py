"""Polymarket domain plugin — the fourth domain (second prediction market).

Registered like kalshi via tools.domain_registry. Serves no AGP Domain
value (event contracts are not FINANCIAL); routes on keywords so a
question like "what does Polymarket price for the next Bitcoin milestone"
picks up these tools.

Tools exposed to the session:
    polymarket_list_markets(closed, limit)
        discover contracts: crypto, geopolitics, culture, science.
    polymarket_get_market(ref)
        one contract by id or slug: question, resolution description
        verbatim, UMA status, last prices, settlement result if resolved.
    polymarket_market_edge(ref, calibrated_prob)
        wires the live two-sided book into tools.edge.MarketQuote and
        assess_edge: calibrated probability vs devigged implied ->
        measured edge + Kelly + CLV anchor. COMPUTES ONLY — never places
        anything, touches no wallet.

The dispatcher runs blocking fetches in an executor; one adapter is
shared process-wide so the 0.5s self-limit holds under bursts.
"""

import asyncio
import logging
import re

from tools.domain_registry import DomainPlugin

logger = logging.getLogger("callisto.polymarket_plugin")

_client = None


def _get_client():
    global _client
    if _client is None:
        from agp.provenance import ProvenanceLedger
        from tools.domains.polymarket.market import SPEC, PolymarketAdapter
        from tools.sources.base import RestSource

        _client = PolymarketAdapter(RestSource(SPEC, ledger=ProvenanceLedger()))
    return _client


LIST_MARKETS_TOOL = {
    "type": "function",
    "function": {
        "name": "polymarket_list_markets",
        "description": (
            "List active Polymarket prediction-market contracts: crypto "
            "milestones, geopolitics, elections, culture, science. Returns "
            "ids/slugs, questions, last traded probabilities, volume. "
            "Read-only public market data."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "closed": {"type": "boolean",
                           "description": "include closed/resolved markets"},
                "limit": {"type": "integer"},
            },
            "required": [],
        },
    },
}

GET_MARKET_TOOL = {
    "type": "function",
    "function": {
        "name": "polymarket_get_market",
        "description": (
            "One Polymarket contract by numeric id or slug: question, the "
            "RESOLUTION CRITERIA verbatim from the market description, UMA "
            "resolution status, last outcome prices, and the settlement "
            "result when resolved."
        ),
        "parameters": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
}

EDGE_TOOL = {
    "type": "function",
    "function": {
        "name": "polymarket_market_edge",
        "description": (
            "Compare a calibrated probability against a Polymarket "
            "contract's live two-sided book: returns devigged market "
            "probability, measured edge, Kelly fractions and EV per unit. "
            "ANALYSIS ONLY — this tool computes an edge assessment and "
            "places no trade, touches no wallet, and cannot."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "ref": {"type": "string"},
                "calibrated_prob": {
                    "type": "number",
                    "description": "the research layer's honest belief, 0-1"},
                "min_edge": {"type": "number",
                             "description": "actionability threshold in "
                                            "probability points (default 0.005)"},
            },
            "required": ["ref", "calibrated_prob"],
        },
    },
}


def _list_payload(args: dict) -> dict:
    page = _get_client().list_markets(
        closed=bool(args.get("closed", False)),
        limit=int(args.get("limit", 50)),
    )
    out = []
    for m in page["markets"]:
        out.append({
            "id": m.id,
            "slug": m.slug,
            "question": m.question,
            "closed": m.closed,
            "uma_resolution_status": m.uma_resolution_status,
            "outcome_prices": list(m.outcome_prices),
            "end_date": m.end_date_iso,
            "volume": m.volume_num,
        })
    return {"markets": out, "_fetch": page["_fetch"]}


def _market_payload(ref: str) -> dict:
    m = _get_client().get_market(str(ref))
    return {
        "id": m.id,
        "slug": m.slug,
        "question": m.question,
        "closed": m.closed,
        "active": m.active,
        "uma_resolution_status": m.uma_resolution_status,
        "outcome_prices": list(m.outcome_prices),
        "resolved_outcome": m.resolved_outcome(),
        "resolution_criteria": m.description,
        "end_date": m.end_date_iso,
        "volume": m.volume_num,
        "liquidity": m.liquidity_num,
        "_fetch": m.raw.get("_fetch"),
    }


def _edge_payload(ref: str, calibrated_prob: float,
                  min_edge: float) -> dict:
    from tools.domains.polymarket.market import polymarket_edge_assessment

    client = _get_client()
    quote, meta = client.market_quote(str(ref))
    a = polymarket_edge_assessment(
        str(calibrated_prob), quote, claim_id=f"polymarket:{meta['id']}",
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
        if name == "polymarket_list_markets":
            return _list_payload(args)
        if name == "polymarket_get_market":
            return _market_payload(args.get("ref", ""))
        if name == "polymarket_market_edge":
            cp = float(args.get("calibrated_prob"))
            if not 0.0 < cp < 1.0:
                raise ValueError("calibrated_prob must be in (0, 1)")
            return _edge_payload(args.get("ref", ""), cp,
                                 float(args.get("min_edge", 0.005)))
        raise ValueError(f"polymarket plugin does not own tool {name!r}")

    try:
        result = await loop.run_in_executor(None, _run, name, dict(arguments))
        result["ok"] = True
        return result
    except Exception as exc:  # surfaced to the model as tool error text
        logger.warning("polymarket tool %s failed: %s", name, exc)
        return {"ok": False, "error": f"{type(exc).__name__}: {exc}",
                "tool": name}


_KEYWORDS = re.compile(
    r"\b(polymarket|prediction markets?|event contracts?|implied probabilit\w+|"
    r"market odds|what does the market (say|think|price)|"
    r"chances? of (a )?(rate cut|recession|shutdown|ceasefire|government))\b",
    re.IGNORECASE,
)


def build_polymarket_plugin() -> DomainPlugin:
    return DomainPlugin(
        name="polymarket",
        domains=set(),   # event contracts are not an AGP Domain value
        keywords=_KEYWORDS,
        tool_schemas=[LIST_MARKETS_TOOL, GET_MARKET_TOOL, EDGE_TOOL],
        freshness=[],    # prices are point-in-time by nature
        execute=_execute,
    )


def register_if_available(registry) -> bool:
    """Register iff the polymarket modules import cleanly (mirrors kalshi)."""
    try:
        import tools.domains.polymarket.market  # noqa: F401
        import tools.resolvers.polymarket  # noqa: F401
    except ImportError:
        logger.info("polymarket plugin unavailable (modules not merged yet)")
        return False
    if "polymarket" not in {p.name for p in registry.plugins()}:
        registry.register(build_polymarket_plugin())
    return True
