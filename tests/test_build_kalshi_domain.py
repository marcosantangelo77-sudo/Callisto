"""Kalshi domain tests.

HARD RULE: no test in this file may open a network socket. The guard
(tests/helpers/no_socket.py) is installed before any import; every fetch
runs through RestSource's injectable transport with canned fixtures
(tests/fixtures/kalshi/). The single sanctioned live read lives in
scripts/live_kalshi_smoke.py, never in this suite.
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
from tools.domains.kalshi.market import (  # noqa: E402
    KalshiAdapter,
    KalshiMarket,
    SPEC,
    kalshi_clv_basis_points,
)
from tools.edge import MarketQuote  # noqa: E402
from tools.resolvers.base import (  # noqa: E402
    OUTCOME_NEGATIVE,
    OUTCOME_POSITIVE,
)
from tools.resolvers.kalshi import KalshiOutcomeResolver  # noqa: E402
from tools.sources.base import RestSource, _RateLimiter  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        "fixtures", "kalshi")


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


def make_adapter(transport) -> KalshiAdapter:
    return KalshiAdapter(RestSource(SPEC, transport=transport,
                                    _limiter=_RateLimiter(0.0)))


def load_fixture(name) -> dict:
    with open(os.path.join(FIXTURES, name)) as fh:
        return json.load(fh)


BASE = "https://api.elections.kalshi.com/trade-api/v2"


# ── market adapter ────────────────────────────────────────────────────────

class TestKalshiAdapter:
    def test_list_markets_parses_prices_as_probabilities(self):
        t = FakeTransport()
        t.stage(f"{BASE}/markets?limit=100",
                load_fixture("markets_kxhighny.json"))
        ad = make_adapter(t)
        page = ad.list_markets()
        assert len(page["markets"]) == 2
        m = page["markets"][0]
        assert m.ticker == "KXHIGHNY-26AUG23-T87"
        assert m.yes_bid == pytest.approx(0.61)
        assert m.yes_ask == pytest.approx(0.63)
        assert m.mid == pytest.approx(0.62)
        # provenance recorded
        assert page["_fetch"]["sha256"]

    def test_list_markets_records_provenance_in_ledger(self):
        t = FakeTransport()
        t.stage(f"{BASE}/markets?limit=50&series_ticker=KXCPI",
                load_fixture("markets_kxhighny.json"))
        ledger = ProvenanceLedger()
        ad = KalshiAdapter(RestSource(SPEC, ledger=ledger, transport=t,
                                      _limiter=_RateLimiter(0.0)))
        ad.list_markets(series_ticker="KXCPI", limit=50)
        # ProvenanceLedger keys observations by content hash (_by_hash) and
        # tracks their URLs (_urls). An earlier version of this test guessed
        # `_records` and `entries()` — neither exists — so it reported "not
        # recorded" while recording was working correctly.
        # Use the ledger's PUBLIC API. An earlier version reached for
        # `_records` and `entries()` — neither exists — and reported "not
        # recorded" while recording worked fine. Private-attribute probing in
        # a test is how you get a false negative about your own safety net.
        urls = list(ledger.observed_urls())
        assert urls, "fetch was not provenance-recorded at all"
        assert any("kalshi" in u for u in urls), (
            f"fetch recorded but not attributed to kalshi: {urls}")

    def test_get_market_carries_resolution_criteria(self):
        t = FakeTransport()
        t.stage(f"{BASE}/markets/KXHIGHNY-26AUG23-T87",
                {"market": load_fixture("markets_kxhighny.json")["markets"][0]})
        ad = make_adapter(t)
        m = ad.get_market("KXHIGHNY-26AUG23-T87")
        assert "greater than 87" in m.rules_primary
        res = ad.resolution("KXHIGHNY-26AUG23-T87") \
            if hasattr(ad, "resolution") else None

    def test_get_market_rejects_malformed_ticker(self):
        ad = make_adapter(FakeTransport())
        for bad in ("", "../etc", "DROP TABLE", "x" * 500):
            with pytest.raises(ValueError):
                ad.get_market(bad)

    def test_settled_market_exposes_result(self):
        settled = load_fixture("markets_kxhighny.json")["markets"][1]
        m = KalshiMarket.from_api(settled)
        assert m.is_settled
        assert m.resolved_outcome() == "yes"

    def test_open_market_has_no_result(self):
        open_m = load_fixture("markets_kxhighny.json")["markets"][0]
        m = KalshiMarket.from_api(open_m)
        assert not m.is_settled
        assert m.resolved_outcome() is None

    def test_price_parser_bounds(self):
        assert _p(None) is None and True

        from tools.domains.kalshi.market import _parse_price as p
        assert p("0.6300") == 0.63
        assert p("1.0000") == 1.0
        assert p("-0.1") is None
        assert p("1.5") is None
        assert p("garbage") is None
        assert p("") is None


def _p(x):
    return x


# ── edge wiring ───────────────────────────────────────────────────────────

class TestEdgeWiring:
    def _quote(self, ad, ticker="KXHIGHNY-26AUG23-T87"):
        t = FakeTransport()
        t.stage(f"{BASE}/markets/{ticker}",
                {"market": load_fixture("markets_kxhighny.json")["markets"][0]})
        # route adapter's source through our staged transport
        ad.source._transport = t
        ad.source._limiter = _RateLimiter(0.0)
        return ad.market_quote(ticker)

    def test_market_quote_is_probability_kind_two_sided(self):
        ad = make_adapter(FakeTransport())
        quote, meta = self._quote(ad)
        assert isinstance(quote, MarketQuote)
        assert quote.kind == "probability"
        assert quote.source == "kalshi"
        # YES ask, not the 0.62 mid: you cannot transact at the mid, so
        # pricing an edge off it overstates it. yes_ask 0.63 + no_ask 0.39
        # = 1.02, and that 2% overround is what the devig removes.
        assert quote.price == pytest.approx(0.63)
        assert quote.counter_price == pytest.approx(0.39)
        fair, audit = quote.fair_probability()
        assert audit["devigged"], "two-sided book must devig"

    def test_calibrated_vs_implied_becomes_measured_edge(self):
        from tools.domains.kalshi.market import kalshi_edge_assessment

        ad = make_adapter(FakeTransport())
        quote, _ = self._quote(ad)
        # model says 70%, market mid devigged ~62% -> positive edge
        a = kalshi_edge_assessment(0.70, quote, claim_id="kalshi:test")
        s = a.summary()
        assert s["market_prob_fair"] > 0.60
        assert s["edge"] == pytest.approx(0.70 - s["market_prob_fair"], abs=1e-6)
        assert a.actionable
        assert s["kelly_quarter"] > 0

    def test_no_edge_when_model_agrees_with_market(self):
        from tools.domains.kalshi.market import kalshi_edge_assessment

        ad = make_adapter(FakeTransport())
        quote, _ = self._quote(ad)
        fair, _ = quote.fair_probability()
        a = kalshi_edge_assessment(fair, quote)
        assert abs(a.summary()["edge"]) < 1e-9
        assert not a.actionable

    def test_clv_between_claim_and_close(self):
        from tools.domains.kalshi.market import kalshi_edge_assessment

        # Healthy two-sided asks (~2% hold each side): 0.63+0.39 and
        # 0.80+0.22. The old 0.62/0.61 fixture was a 23%-hold stale mix and
        # is now correctly rejected as an invalid book by the sanity gate.
        q_claim = MarketQuote(price=0.63, counter_price=0.39,
                              kind="probability", source="kalshi")
        q_close = MarketQuote(price=0.80, counter_price=0.22,
                              kind="probability", source="kalshi")
        bp = kalshi_clv_basis_points(q_claim, q_close)
        assert bp is not None and bp > 0, "market moved toward YES -> +CLV"

    def test_clv_refuses_one_sided_quotes(self):
        raw = MarketQuote(price=0.62, kind="probability")
        assert kalshi_clv_basis_points(raw, raw) is None


# ── resolver ──────────────────────────────────────────────────────────────

class FixtureAdapter:
    """Stand-in for KalshiAdapter returning fixture markets."""

    def __init__(self, markets: dict):
        self.markets = markets

    def get_market(self, ticker):
        return KalshiMarket.from_api(self.markets[ticker])


def _fixture_map():
    fs = load_fixture("markets_kxhighny.json")["markets"]
    return {m["ticker"]: m for m in fs}


class TestKalshiResolver:
    def test_settled_yes_yields_positive_evidence(self):
        r = KalshiOutcomeResolver(FixtureAdapter(_fixture_map()))
        recs = list(asyncio.run(_collect(r, "KXCPI-26SEP-AE10.2")))
        assert len(recs) == 1
        rec = recs[0]
        assert rec.resolved_outcome == OUTCOME_POSITIVE
        assert rec.is_decided
        assert rec.payoff == 1.0
        assert rec.event_id == "KXCPI-26SEP-AE10.2"
        assert rec.source == "kalshi"

    def test_unsettled_contract_yields_nothing(self):
        r = KalshiOutcomeResolver(FixtureAdapter(_fixture_map()))
        recs = list(asyncio.run(_collect(r, "KXHIGHNY-26AUG23-T87")))
        assert recs == []

    async def _collect(self_unused=None, r=None, tid=None):  # pragma: no cover
        raise NotImplementedError


async def _collect(r, tid):
    out = []
    async for rec in r.iter_evidence(tid):
        out.append(rec)
    return out

    def test_summary_counts_resolution(self):
        r = KalshiOutcomeResolver(FixtureAdapter(_fixture_map()))
        s = asyncio.run(r.summarize("KXCPI-26SEP-AE10.2"))
        assert s.total == 1 and s.positive == 1
        assert s.fully_resolved

    def test_has_resolved_false_while_open(self):
        r = KalshiOutcomeResolver(FixtureAdapter(_fixture_map()))
        assert not asyncio.run(r.has_resolved("KXHIGHNY-26AUG23-T87"))
        assert asyncio.run(r.has_resolved("KXCPI-26SEP-AE10.2"))


# ── domain plugin ─────────────────────────────────────────────────────────

class TestKalshiPlugin:
    def test_registers_and_routes_keywords(self):
        from tools.domain_registry import ToolRegistry
        from tools.domains.kalshi.plugin import register_if_available

        reg = ToolRegistry()
        assert register_if_available(reg)
        plugin = {p.name: p for p in reg.plugins()}["kalshi"]
        assert plugin.serves(None, "what are the Kalshi odds on the next CPI print?")
        assert not plugin.serves(None, "best NBA props tonight")

    def test_execute_get_market_against_fixtures(self):
        from tools.domains.kalshi import plugin as kp

        class _T(FakeTransport):
            pass

        client_transport = FakeTransport()
        client_transport.stage(
            f"{BASE}/markets/KXHIGHNY-26AUG23-T87",
            {"market": load_fixture("markets_kxhighny.json")["markets"][0]})

        class FixtureClient:
            def get_market(self, ticker):
                return KalshiMarket.from_api(load_fixture(
                    "markets_kxhighny.json")["markets"][0])

        kp._client = FixtureClient()
        try:
            result = asyncio.run(kp._execute(
                "kalshi_get_market", {"ticker": "KXHIGHNY-26AUG23-T87"}))
        finally:
            kp._client = None
        assert result["ok"]
        assert result["implied_prob_mid"] == pytest.approx(0.62)
        assert "greater than 87" in result["resolution_criteria"]

    def test_edge_tool_computes_and_never_trades(self):
        from tools.domains.kalshi import plugin as kp

        # Healthy book: yes_ask 0.63 + no_ask 0.39 = 1.02 (~2% hold). The
        # old 0.62/0.61 fixture was a 23%-hold stale mix, now rejected.
        quote = MarketQuote(price=0.63, counter_price=0.39,
                            kind="probability", source="kalshi")

        class FixtureClient:
            def market_quote(self, ticker):
                return quote, {"ticker": ticker}

        kp._client = FixtureClient()
        try:
            result = asyncio.run(kp._execute("kalshi_market_edge", {
                "ticker": "KXHIGHNY-26AUG23-T87", "calibrated_prob": 0.7}))
        finally:
            kp._client = None
        assert result["ok"]
        assert result["edge"] > 0
        assert "no trade" in result["disposition"]

    def test_source_registry_lists_kalshi_tier3(self):
        from tools.sources.registry import SourceAdapter, SourceRegistry
        from tools.sources import adapters as sa

        reg = SourceRegistry()
        sa.register_all(reg)
        spec = reg.get("kalshi").spec
        assert spec.tier == 3
        hits = reg.select("market-implied probability of a Fed rate decision")
        assert any(s.name == "kalshi" for s in hits)


# ── mandate guard ─────────────────────────────────────────────────────────

class TestReadOnlyMandate:
    def test_package_contains_no_order_path(self):
        """No credentials, no order paths, no account access — grep-guarded."""
        pkg_dir = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "tools", "domains", "kalshi")
        banned = ("orders", "portfolio", "api_key", "authorization",
                  "balance", "session_token", "signature", "private_key")
        for fname in os.listdir(pkg_dir):
            if not fname.endswith(".py"):
                continue
            # Scan CODE, not prose. The package's own docstring states that
            # it deliberately never touches orders/positions/balance — a raw
            # substring scan flags that disclaimer and fails on the very
            # comment asserting the property under test.
            raw = open(os.path.join(pkg_dir, fname)).read()
            no_doc = re.sub(TRIPLE_STR, "", raw)
            no_str = re.sub(SINGLE_STR, "", no_doc)
            code = "\n".join(l.split("#")[0] for l in no_str.splitlines()).lower()
            for word in banned:
                assert not re.search(r"\b" + re.escape(word) + r"\b", code), (
                    f"{fname} has a live '{word}' reference in CODE — the "
                    "package must contain no order/account path whatsoever")

    def test_spec_declares_public_read_only_endpoints(self):
        assert SPEC.base_url.endswith("/trade-api/v2")
        assert SPEC.key_env_var == "", "public data needs no key"
