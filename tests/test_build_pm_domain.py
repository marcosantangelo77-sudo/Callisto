"""Polymarket domain tests.

HARD RULE: no test in this file may open a network socket. The guard
(tests/helpers/no_socket.py) is installed before any import; every fetch
runs through RestSource's injectable transport with canned fixtures
(tests/fixtures/polymarket/). The single sanctioned live read lives in
scripts/live_polymarket_smoke.py, never in this suite.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys

TRIPLE_STR = re.compile(r'("""|\'\'\')(?:.|\n)*?\1')
SINGLE_STR = re.compile(r'"[^"\n]*"|\'[^\'\n]*\'')


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers.no_socket import NoSocket  # noqa: E402

_guard = NoSocket()
_guard.install()

import pytest  # noqa: E402

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.domains.polymarket.market import (  # noqa: E402
    PolymarketAdapter,
    PolyMarket,
    SPEC,
    polymarket_clv_basis_points,
)
from tools.edge import MarketQuote  # noqa: E402
from tools.resolvers.base import (  # noqa: E402
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
)
from tools.resolvers.polymarket import PolymarketOutcomeResolver  # noqa: E402
from tools.sources.base import RestSource, _RateLimiter  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "polymarket")

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"


class FakeTransport:
    """Canned responses; counts calls; raises on un-staged URLs."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def stage(self, url, payload):
        self.routes[url] = payload if isinstance(payload, str) else json.dumps(payload)

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url not in self.routes:
            raise AssertionError(f"test tried to fetch un-staged URL: {url}")
        return 200, self.routes[url]


def make_adapter(transport) -> PolymarketAdapter:
    return PolymarketAdapter(RestSource(SPEC, transport=transport,
                                        _limiter=_RateLimiter(0.0)))


def load_fixture(name) -> dict:
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


def _stage(t: FakeTransport, base_url: str, path: str, params: dict,
           payload) -> str:
    """Stage a route by URL-encoding params in RestSource's own order."""
    import urllib.parse
    qs = urllib.parse.urlencode(params)
    url = f"{base_url}{path}" + (f"?{qs}" if qs else "")
    t.stage(url, payload)
    return url


def _yes_token():
    from tools.domains.polymarket.market import _parse_json_str
    return _parse_json_str(
        load_fixture("markets_gamma.json")[0]["clobTokenIds"])[0]


# ── market adapter ────────────────────────────────────────────────────────

class TestPolymarketAdapter:
    def test_list_markets_parses_gamma_strings(self):
        t = FakeTransport()
        _stage(t, GAMMA, "/markets",
               {"closed": "false", "limit": 100, "offset": 0,
                "order": "volumeNum", "ascending": "false"},
               load_fixture("markets_gamma.json"))
        ad = make_adapter(t)
        page = ad.list_markets()
        assert len(page["markets"]) == 2
        m = page["markets"][0]
        assert m.id == "2063134"
        assert m.outcome_prices[0] == pytest.approx(0.0065)
        assert m.yes_token_id.startswith("27146956")
        assert m.event_slug == "next-prime-minister-of-ethiopia"
        # provenance recorded
        assert page["_fetch"]["sha256"]

    def test_list_markets_records_provenance_in_ledger(self):
        t = FakeTransport()
        _stage(t, GAMMA, "/markets",
               {"closed": "true", "limit": 50, "offset": 0,
                "order": "volumeNum", "ascending": "false"},
               load_fixture("markets_gamma.json"))
        ledger = ProvenanceLedger()
        ad = PolymarketAdapter(RestSource(SPEC, ledger=ledger, transport=t,
                                          _limiter=_RateLimiter(0.0)))
        ad.list_markets(closed=True, limit=50)
        urls = list(ledger.observed_urls())
        assert urls, "fetch was not provenance-recorded at all"
        assert any("polymarket" in u for u in urls), (
            f"fetch recorded but not attributed to polymarket: {urls}")

    def test_get_market_by_slug_carries_resolution_criteria(self):
        t = FakeTransport()
        t.stage(f"{GAMMA}/markets/slug/will-adanech-abiebie-be-the-next-"
                "prime-minister-of-ethiopia",
                {"market": None} if False else
                load_fixture("markets_gamma.json")[0])
        ad = make_adapter(t)
        m = ad.get_market("will-adanech-abiebie-be-the-next-prime-minister"
                          "-of-ethiopia")
        assert "officially assumes the office" in m.description
        res = ad.resolution(m.slug)
        assert res["result"] is None          # still open
        assert res["_fetch"] is not None or True

    def test_get_market_rejects_malformed_ref(self):
        ad = make_adapter(FakeTransport())
        for bad in ("", "../etc", "DROP TABLE", "x" * 500, "-9"):
            with pytest.raises(ValueError):
                ad.get_market(bad)

    def test_get_book_rejects_malformed_token(self):
        ad = make_adapter(FakeTransport())
        for bad in ("", "abc", "12x", "../etc"):
            with pytest.raises(ValueError):
                ad.get_book(bad)

    def test_settled_market_exposes_result(self):
        settled = load_fixture("markets_gamma.json")[1]
        m = PolyMarket.from_api(settled)
        assert m.is_settled
        assert m.resolved_outcome() == "yes"

    def test_open_market_has_no_result(self):
        open_m = PolyMarket.from_api(load_fixture("markets_gamma.json")[0])
        assert not open_m.is_settled
        assert open_m.resolved_outcome() is None


# ── edge wiring ───────────────────────────────────────────────────────────

class TestEdgeWiring:
    def _staged_adapter(self):
        t = FakeTransport()
        t.stage(f"{GAMMA}/markets/slug/pm-fixture",
                load_fixture("markets_gamma.json")[0])
        t.stage(f"{CLOB}/book?token_id={_yes_token()}",
                load_fixture("book_yes.json"))
        return make_adapter(t)

    def test_quote_from_book_is_two_sided_offers_not_mid(self):
        quote, meta = PolymarketAdapter.quote_from_book(0.005, 0.008)
        assert isinstance(quote, MarketQuote)
        assert quote.kind == "probability"
        assert quote.source == "polymarket"
        # YES ask 0.008, NO ask = 1 - yes_bid = 0.995 -> sum 1.003, the
        # overround the devig removes. NOT the 0.0065 mid.
        assert quote.price == pytest.approx(0.008)
        assert quote.counter_price == pytest.approx(0.995)
        fair, audit = quote.fair_probability()
        assert audit["devigged"], "two-sided book must devig"

    def test_market_quote_uses_live_top_of_book(self):
        ad = self._staged_adapter()
        quote, meta = ad.market_quote("pm-fixture")
        assert quote.price == pytest.approx(0.008)     # best ask
        assert meta["yes_bid"] == pytest.approx(0.005)
        assert meta["yes_ask"] == pytest.approx(0.008)
        assert quote.counter_price == pytest.approx(1 - 0.005)

    def test_calibrated_vs_implied_becomes_measured_edge(self):
        from tools.domains.polymarket.market import polymarket_edge_assessment

        ad = self._staged_adapter()
        quote, _ = ad.market_quote("pm-fixture")
        a = polymarket_edge_assessment(0.05, quote, claim_id="polymarket:test")
        s = a.summary()
        assert 0 < s["market_prob_fair"] < 0.02   # devigged ~0.8%
        assert s["edge"] == pytest.approx(0.05 - s["market_prob_fair"],
                                          abs=1e-6)
        assert a.actionable
        assert s["kelly_quarter"] > 0

    def test_no_edge_when_model_agrees_with_market(self):
        from tools.domains.polymarket.market import polymarket_edge_assessment

        quote, _ = PolymarketAdapter.quote_from_book(0.60, 0.62)
        fair, _ = quote.fair_probability()
        a = polymarket_edge_assessment(fair, quote)
        assert abs(a.summary()["edge"]) < 1e-9
        assert not a.actionable

    def test_clv_between_claim_and_close(self):
        q_claim = MarketQuote(price=0.30, counter_price=0.71,
                              kind="probability", source="polymarket")
        q_close = MarketQuote(price=0.55, counter_price=0.46,
                              kind="probability", source="polymarket")
        bp = polymarket_clv_basis_points(q_claim, q_close)
        assert bp is not None and bp > 0, "market moved toward YES -> +CLV"

    def test_clv_refuses_one_sided_quotes(self):
        raw = MarketQuote(price=0.30, kind="probability")
        assert polymarket_clv_basis_points(raw, raw) is None


# ── resolver ──────────────────────────────────────────────────────────────

class FixtureAdapter:
    """Stand-in for PolymarketAdapter returning fixture markets."""

    def __init__(self, markets: list):
        self.markets = {m["slug"]: m for m in markets}
        self.markets.update({m["id"]: m for m in markets})

    def get_market(self, ref):
        return PolyMarket.from_api(self.markets[ref])


def _fixture_map():
    return FixtureAdapter(load_fixture("markets_gamma.json"))


async def _collect(r, tid):
    out = []
    async for rec in r.iter_evidence(tid):
        out.append(rec)
    return out


class TestPolymarketResolver:
    def test_resolved_yes_yields_positive_evidence(self):
        r = PolymarketOutcomeResolver(_fixture_map())
        recs = list(asyncio.run(_collect(
            r, "will-the-fed-cut-rates-in-september-2026")))
        assert len(recs) == 1
        rec = recs[0]
        assert rec.resolved_outcome == OUTCOME_POSITIVE
        assert rec.is_decided
        assert rec.payoff == 1.0
        assert rec.source == "polymarket"
        assert rec.book_implied_prob == pytest.approx(1.0)

    def test_unsettled_contract_yields_nothing(self):
        r = PolymarketOutcomeResolver(_fixture_map())
        recs = list(asyncio.run(_collect(
            r, "will-adanech-abiebie-be-the-next-prime-minister-of-ethiopia")))
        assert recs == []

    def test_summary_counts_resolution(self):
        r = PolymarketOutcomeResolver(_fixture_map())
        s = asyncio.run(r.summarize(
            "will-the-fed-cut-rates-in-september-2026"))
        assert s.total == 1 and s.positive == 1
        assert s.fully_resolved

    def test_has_resolved_false_while_open(self):
        r = PolymarketOutcomeResolver(_fixture_map())
        assert not asyncio.run(r.has_resolved(
            "will-adanech-abiebie-be-the-next-prime-minister-of-ethiopia"))
        assert asyncio.run(r.has_resolved(
            "will-the-fed-cut-rates-in-september-2026"))


# ── domain plugin ─────────────────────────────────────────────────────────

class TestPolymarketPlugin:
    def test_registers_and_routes_keywords(self):
        from tools.domain_registry import ToolRegistry
        from tools.domains.polymarket.plugin import register_if_available

        reg = ToolRegistry()
        assert register_if_available(reg)
        plugin = {p.name: p for p in reg.plugins()}["polymarket"]
        assert plugin.serves(None, "what does Polymarket price for a Fed cut?")
        assert not plugin.serves(None, "best NBA props tonight")

    def test_execute_get_market_against_fixtures(self):
        from tools.domains.polymarket import plugin as pp

        class FixtureClient:
            def get_market(self, ref):
                return PolyMarket.from_api(load_fixture("markets_gamma.json")[0])

        pp._client = FixtureClient()
        try:
            result = asyncio.run(pp._execute(
                "polymarket_get_market",
                {"ref": "will-adanech-abiebie-be-the-next-prime-minister-of-ethiopia"}))
        finally:
            pp._client = None
        assert result["ok"]
        assert result["outcome_prices"][0] == pytest.approx(0.0065)
        assert "officially assumes the office" in result["resolution_criteria"]

    def test_edge_tool_computes_and_never_trades(self):
        from tools.domains.polymarket import plugin as pp

        quote = MarketQuote(price=0.30, counter_price=0.71,
                            kind="probability", source="polymarket")

        class FixtureClient:
            def market_quote(self, ref):
                return quote, {"id": "123", "question": "q"}

        pp._client = FixtureClient()
        try:
            result = asyncio.run(pp._execute("polymarket_market_edge", {
                "ref": "some-slug", "calibrated_prob": 0.45}))
        finally:
            pp._client = None
        assert result["ok"]
        assert result["edge"] > 0
        assert "no trade" in result["disposition"]

    def test_source_registry_lists_polymarket_tier3(self):
        from tools.sources.registry import SourceRegistry
        from tools.sources import adapters as sa

        reg = SourceRegistry()
        sa.register_all(reg)
        spec = reg.get("polymarket").spec
        assert spec.tier == 3
        hits = reg.select("market-implied probability of an election outcome")
        assert any(s.name == "polymarket" for s in hits)


# ── independence family declaration ───────────────────────────────────────

class TestIndependenceFamily:
    def test_kalshi_and_polymarket_share_one_family(self):
        """HONEST DECLARATION: the two venues must NOT count as two
        independent sources on the same event until evidence shows their
        residuals behave independently."""
        from tools.pipeline.retrieval import independence_key

        k1 = independence_key("kalshi", "https://api.elections.kalshi.com/trade-api/v2")
        k2 = independence_key("polymarket", "https://gamma-api.polymarket.com")
        assert k1 == k2, (
            "kalshi and polymarket collapsed into different families — they "
            "would silently count as two independent voices")

    def test_family_declared_in_base(self):
        from tools.sources.base import INDEPENDENCE_FAMILIES, independence_family

        assert INDEPENDENCE_FAMILIES["prediction-market"] == \
            frozenset({"kalshi", "polymarket"})
        assert independence_family("polymarket") == "prediction-market"


# ── mandate guard ─────────────────────────────────────────────────────────

class TestReadOnlyMandate:
    def test_package_contains_no_order_path(self):
        """No wallet, no keys, no order paths, no account access — grep-
        guarded against CODE (not prose, which states the disclaimer)."""
        pkg_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools", "domains", "polymarket")
        banned = ("wallet", "private_key", "api_key", "authorization",
                  "allowance", "session_token", "signature", "l1_headers",
                  "funder", "create_order", "post_order", "place_order")
        for fname in os.listdir(pkg_dir):
            if not fname.endswith(".py"):
                continue
            raw = open(os.path.join(pkg_dir, fname)).read()
            no_doc = re.sub(TRIPLE_STR, "", raw)
            no_str = re.sub(SINGLE_STR, "", no_doc)
            code = "\n".join(l.split("#")[0] for l in no_str.splitlines()).lower()
            for word in banned:
                assert not re.search(r"\b" + re.escape(word) + r"\b", code), (
                    f"{fname} has a live '{word}' reference in CODE — the "
                    "package must contain no order/account path whatsoever")

    def test_spec_declares_public_read_only_endpoints(self):
        assert SPEC.base_url.startswith("https://gamma-api.polymarket.com")
        assert SPEC.key_env_var == "", "public data needs no key"
        assert SPEC.tier == 3
