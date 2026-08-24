"""Fed + PubMed adapter parse tests — fixtures only, no network.

HARD RULE: no test here may open a socket. Fixtures were captured from
the LIVE endpoints (federalreserve.gov /feeds/*.xml and NCBI e-utilities)
so the parser is proven against reality, not an invention. The live path
is exercised separately by tools/sources/health.py probes (task 61),
never by this offline suite.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.helpers.no_socket import NoSocket  # noqa: E402

_guard = NoSocket()
_guard.install()

import pytest  # noqa: E402

from tools.sources.base import RestSource, _RateLimiter  # noqa: E402
from tools.sources.federalreserve import (  # noqa: E402
    SPEC as FED_SPEC,
    FederalReserveAdapter,
    parse_feed,
)
from tools.sources.pubmed import (  # noqa: E402
    SPEC as PUBMED_SPEC,
    PubMedAdapter,
)
from tools.sources.query_builder import build_plan  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")


def load(rel: str) -> str:
    with open(os.path.join(FIXTURES, rel), encoding="utf-8") as fh:
        return fh.read()


class FakeTransport:
    """Canned responses; raises on un-staged URLs (no socket possible)."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def stage(self, url, body):
        self.routes[url] = body

    def __call__(self, url, headers):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError(f"un-staged URL fetched: {url}")
        return 200, self.routes[url]


def make(source_spec, transport):
    return type("A", (), {})  # unused; adapters built inline below


# ── federalreserve ────────────────────────────────────────────────────────

class TestFederalReserve:
    def test_parse_speeches_fixture(self):
        t = FakeTransport()
        base = FED_SPEC.base_url
        t.stage(base + "/feeds/speeches.xml", load("fed/speeches.xml"))
        ad = FederalReserveAdapter(RestSource(FED_SPEC, transport=t,
                                              _limiter=_RateLimiter(0.0)))
        speeches = ad.recent_speeches()
        assert len(speeches) >= 5
        first = speeches[0]
        assert first["title"] == ("Cook, Outlook for the U.S. and Alaskan "
                                  "Economies")
        assert first["url"].startswith(
            "https://www.federalreserve.gov/newsevents/speech/")
        assert first["pub_date_gmt"].endswith("GMT")
        assert "2026" in first["pub_date_gmt"]
        # one fetch, the feed URL — nothing else touched
        assert t.calls == [base + "/feeds/speeches.xml"]

    def test_parse_press_feed_and_monetary_filter(self):
        t = FakeTransport()
        base = FED_SPEC.base_url
        t.stage(base + "/feeds/press_all.xml", load("fed/press_all.xml"))
        ad = FederalReserveAdapter(RestSource(FED_SPEC, transport=t,
                                              _limiter=_RateLimiter(0.0)))
        items = ad.monetary_policy_items()
        assert any("FOMC statement" in i["title"] for i in items)
        assert all(i["category"] == "Monetary Policy" for i in items)
        fomc = next(i for i in items if "FOMC statement" in i["title"])
        assert fomc["pub_date_gmt"] == "Wed, 29 Jul 2026 18:00:00 GMT"

    def test_html_error_page_is_rejected_not_zero_results(self):
        with pytest.raises(ValueError):
            parse_feed("<!doctype html><html><body>blocked</body></html>")
        # a VALID xml doc that isn't a feed must also be rejected
        with pytest.raises(ValueError, match="expected <rss>"):
            parse_feed("<?xml version='1.0'?><foo/>")

    def test_query_builder_plans_fomc_question(self):
        plan = build_plan("federalreserve",
                          "what did the latest FOMC statement say about "
                          "inflation?")
        assert plan.plannable
        methods = [q.method for q in plan.queries]
        assert "monetary_policy_items" in methods

    def test_registry_selects_fed_for_monetary_questions(self):
        from tools.sources.registry import SourceRegistry
        from tools.sources import adapters as sa
        reg = SourceRegistry()
        sa.register_all(reg)
        spec = reg.get("federalreserve").spec
        assert spec.tier == 2  # primary documents
        hits = reg.select("Federal Reserve speeches and FOMC statements "
                          "on interest rates")
        assert any(s.name == "federalreserve" for s in hits)


# ── pubmed ────────────────────────────────────────────────────────────────

class TestPubMed:
    def test_search_and_summarize_against_live_captured_fixtures(self):
        esearch = load("pubmed/esearch_semaglutide.json")
        esum = load("pubmed/esummary_one.json")
        pmid = json.loads(esearch)["esearchresult"]["idlist"][0]
        t = FakeTransport()
        base = PUBMED_SPEC.base_url
        probe_src = RestSource(PUBMED_SPEC, transport=t,
                               _limiter=_RateLimiter(0.0))
        t.stage(probe_src.build_url("/esearch.fcgi", {
                    "db": "pubmed", "term": "semaglutide cardiovascular "
                                           "outcomes", "retmax": 10,
                    "retmode": "json", "sort": "date"}), esearch)
        t.stage(base + f"/esummary.fcgi?db=pubmed&id={pmid}&retmode=json",
                esum)
        ad = PubMedAdapter(probe_src)
        res = ad.search("semaglutide cardiovascular outcomes", limit=10)
        assert res["count"] > 500
        assert pmid in res["pmids"]
        summ = ad.summarize([pmid])
        rec = summ[pmid]
        assert rec["title"]
        assert rec["journal"]
        assert isinstance(rec["pubtypes"], list)

    def test_esearch_garbage_payload_raises(self):
        t = FakeTransport()
        base = PUBMED_SPEC.base_url
        t.stage(base + "/esearch.fcgi?db=pubmed&retmax=5&retmode=json"
                       "&sort=date&term=x", "<html>rate limited</html>")
        ad = PubMedAdapter(RestSource(PUBMED_SPEC, transport=t,
                                      _limiter=_RateLimiter(0.0)))
        with pytest.raises(Exception):
            ad.search("x", limit=5)  # non-JSON must not parse to zero rows

    def test_query_builder_plans_pubmed(self):
        plan = build_plan(
            "pubmed", "what published results exist for semaglutide "
            "phase 3 cardiovascular outcomes?")
        assert plan.plannable
        q = plan.queries[0]
        assert q.source == "pubmed" and q.method == "search"
        assert "semaglutide" in q.kwargs["query"]

    def test_registry_lists_pubmed_as_secondary_tier(self):
        from tools.sources.registry import SourceRegistry
        from tools.sources import adapters as sa
        reg = SourceRegistry()
        sa.register_all(reg)
        spec = reg.get("pubmed").spec
        assert spec.tier == 4  # secondary to the trial itself
        assert spec.key_env_var == ""  # free, no key
        hits = reg.select("published results for a phase 2 oncology trial")
        assert any(s.name == "pubmed" for s in hits)

    def test_health_probes_registered_for_both_sources(self):
        # The LIVE path belongs to the source-health check (task 61);
        # this only proves probes exist so run_all() covers both.
        from tools.sources import health
        assert "federalreserve" in health.PROBES
        assert "pubmed" in health.PROBES
