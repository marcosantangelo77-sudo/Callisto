"""Wave-3 source registry tests (P3).

HARD RULE inherited from R4: no live API calls. The no-socket guard is
installed before any import; every fetch runs through RestSource's
injectable transport with canned fixtures. The SEC block incident must
never be reproducible from a test run.
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

from tools.sources.base import (  # noqa: E402
    PROVENANCE_TIERS,
    RestSource,
    SourceError,
    SourceSpec,
    _RateLimiter,
)
from tools.sources.registry import (  # noqa: E402
    SelectionDecision,
    SourceAdapter,
    SourceRegistry,
)

SEC_BASE = "https://efts.sec.gov/LATEST/search-index"
USPTO_BASE = "https://api.uspto.gov/api/v1"
EIA_BASE = "https://api.eia.gov/v2"
S2_BASE = "https://api.semanticscholar.org/graph/v1"
WAYBACK_BASE = "https://archive.org/wayback"
SNAP_URL = ("https://web.archive.org/web/20230515000000/"
            "https://example.com/pricing")


class FakeTransport:
    def __init__(self):
        self.routes = {}
        self.calls = []

    def stage(self, url, payload):
        self.routes[url] = (payload if isinstance(payload, str)
                            else json.dumps(payload))

    def __call__(self, url, headers):
        self.calls.append((url, dict(headers)))
        if url not in self.routes:
            raise AssertionError(f"test tried to fetch un-staged URL: {url}")
        return 200, self.routes[url]


def make_source(spec, transport):
    return RestSource(spec, ledger=None, transport=transport,
                      _limiter=_RateLimiter(0.0))


def build(module_name, cls_name, routes):
    import importlib
    mod = importlib.import_module(f"tools.sources.{module_name}")
    cls = getattr(mod, cls_name)
    t = FakeTransport()
    for url, payload in routes.items():
        t.stage(url, payload)
    return cls(make_source(mod.SPEC, t)), t


def registry_with(*pairs):
    reg = SourceRegistry()
    for m, c in pairs:
        mod = __import__(f"tools.sources.{m}", fromlist=[c])
        reg.register(SourceAdapter(spec=mod.SPEC, make_adapter=getattr(mod, c)))
    return reg


# ── Job 1: CourtListener ──────────────────────────────────────────────────

class TestCourtListener:
    def test_token_header_sent_from_env(self, monkeypatch):
        cl, t = build("courtlistener", "CourtListenerAdapter", {
            "https://www.courtlistener.com/api/rest/v4/search/"
            "?q=test&type=o&page_size=20": {"count": 1, "next": None,
                                            "results": []},
        })
        monkeypatch.setenv("CALLISTO_COURTLISTENER_TOKEN", "tok123")
        cl.search("test")
        _, headers = t.calls[0]
        assert headers["Authorization"] == "Token tok123"

    def test_missing_token_raises_before_fetch(self):
        cl, t = build("courtlistener", "CourtListenerAdapter", {})
        with pytest.raises(SourceError, match="CALLISTO_COURTLISTENER_TOKEN"):
            cl.search("test")
        assert t.calls == []          # nothing hit the network

    def test_page_size_capped_at_20_for_free_quota(self):
        cl, t = build("courtlistener", "CourtListenerAdapter", {
            "https://www.courtlistener.com/api/rest/v4/search/"
            "?q=x&type=o&page_size=20": {"count": 0, "next": None,
                                         "results": []},
        })
        cl.source.api_key = lambda: "k"
        cl.search("x", page_size=500)
        assert "page_size=20" in t.calls[0][0]

    def test_paginate_follows_next_cursor_verbatim(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_COURTLISTENER_TOKEN", "tok")
        next_url = ("https://www.courtlistener.com/api/rest/v4/search/"
                    "?cursor=cz04&q=x&type=o")
        cl, _t = build("courtlistener", "CourtListenerAdapter", {
            "https://www.courtlistener.com/api/rest/v4/search/"
            "?q=x&type=o&page_size=20": {
                "count": 2, "next": next_url,
                "results": [{"id": 1}]},
            next_url: {"count": 2, "next": None, "results": [{"id": 2}]},
        })
        results = cl.search_all("x")
        assert [r["id"] for r in results] == [1, 2]

    def test_cluster_and_citation_lookup_paths(self):
        cl, _t = build("courtlistener", "CourtListenerAdapter", {
            "https://www.courtlistener.com/api/rest/v4/clusters/42/":
                {"id": 42},
            "https://www.courtlistener.com/api/rest/v4/citation-lookup/"
            "?citation=citation": {"results": []},
        })
        cl.source.api_key = lambda: "k"
        assert cl.cluster(42)["id"] == 42
        cl.cite_lookup("citation")
        assert "citation-lookup" in _t.calls[-1][0]


# ── Job 1: USPTO ODP ──────────────────────────────────────────────────────

class TestUsptoOdp:
    def test_api_key_header(self, monkeypatch):
        us, t = build("uspto_odp", "UsptoOdpAdapter", {
            USPTO_BASE + "/patent/applications/search"
            "?q=title%3Alidar&offset=0&limit=25":
                {"totalResults": 0, "results": []},
        })
        monkeypatch.setenv("CALLISTO_USPTO_ODP_KEY", "odpkey")
        us.search_applications("title:lidar")
        _, headers = t.calls[0]
        assert headers["X-API-KEY"] == "odpkey"
        # key never leaks into the query string
        assert "odpkey" not in t.calls[0][0]

    def test_missing_key_raises_before_fetch(self):
        us, t = build("uspto_odp", "UsptoOdpAdapter", {})
        with pytest.raises(SourceError, match="CALLISTO_USPTO_ODP_KEY"):
            us.search_applications("q")
        assert t.calls == []

    def test_post_search_body_shape(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_USPTO_ODP_KEY", "k")
        us, _t = build("uspto_odp", "UsptoOdpAdapter", {
            USPTO_BASE + "/patent/applications/search":
                {"totalResults": 1, "results": [{"applicationNumber": "17"}]},
        })
        out = us.search_applications_post(
            "applicationMetaData.applicationTypeLabelName:Utility")
        assert out["totalResults"] == 1

    def test_application_number_validated_locally(self):
        us, t = build("uspto_odp", "UsptoOdpAdapter", {})
        with pytest.raises(ValueError, match="application number"):
            us.application("not-a-number")
        assert t.calls == []


# ── Job 2: the new tier-1 macro/firm sources ──────────────────────────────

class TestSecFullText:
    def test_search_normalizes_hits(self):
        sft, _t = build("sec_fts", "SecFullTextAdapter", {
            SEC_BASE + "?q=solar+tariff&dateRange=custom&startdt=2020-01-01"
                       "&enddt=2021-01-01&forms=10-K": {
                "hits": {"total": {"value": 7}, "hits": [
                    {"_source": {"_id": "0000320193-20-000096:aapl-10k.htm",
                                 "ciks": [320193],
                                 "file_type": "10-K",
                                 "file_date": "2020-07-30",
                                 "display_names": ["Apple Inc."]}}],
                }},
        })
        out = sft.search("solar tariff", start="2020-01-01",
                         end="2021-01-01", forms="10-K")
        assert out["total"] == 7
        h = out["hits"][0]
        assert h["accession"] == "0000320193-20-000096"
        assert h["filename"] == "aapl-10k.htm"
        assert h["company"] == "Apple Inc."
        assert "_fetch" in out and out["_fetch"]["sha256"]

    def test_empty_query_rejected_without_fetch(self):
        sft, t = build("sec_fts", "SecFullTextAdapter", {})
        with pytest.raises(ValueError, match="non-empty"):
            sft.search("   ")
        assert t.calls == []


class TestBea:
    def test_get_data_requires_key_and_builds_params(self, monkeypatch):
        bea, t = build("bea", "BeaAdapter", {
            "https://apps.bea.gov/api/data/?UserID=k&ResultFormat=JSON"
            "&method=GetData&DataSetName=NIPA&TableName=T10101"
            "&LineCode=1&Frequency=A&Year=2023": {
                "BEAAPIs": {"Results": {"Data": [
                    {"DataValue": "22671.0", "TimePeriod": "2023"}]}}},
        })
        monkeypatch.setenv("CALLISTO_BEA_API_KEY", "k")
        out = bea.get_data("NIPA", tablename="T10101", linecode=1,
                           frequency="A", years="2023")
        data = out["BEAAPIs"]["Results"]["Data"]
        assert data[0]["TimePeriod"] == "2023"
        assert "_fetch" in out

    def test_missing_key_no_fetch(self):
        bea, t = build("bea", "BeaAdapter", {})
        with pytest.raises(SourceError, match="CALLISTO_BEA_API_KEY"):
            bea.get_data("NIPA", tablename="T10101", linecode=1)
        assert t.calls == []


class TestCensus:
    def test_flat_array_normalized(self, monkeypatch):
        cen, _t = build("census", "CensusAdapter", {
            "https://api.census.gov/data/2023/timeseries/eits/resconst"
            "?get=cell_value%2Ctime_slot_id&for=us%3A%2A&key=ck": [
                ["cell_value", "time_slot_id"],
                ["1309", "1"], ["1288", "1"]],
        })
        monkeypatch.setenv("CALLISTO_CENSUS_API_KEY", "ck")
        out = cen.query("2023", "timeseries/eits/resconst",
                        ["cell_value", "time_slot_id"], geo_for="us:*")
        assert out["columns"][0] == "cell_value"
        assert len(out["rows"]) == 2

    def test_bad_shape_raises(self):
        cen, _t = build("census", "CensusAdapter", {
            "https://api.census.gov/data/x/y?get=a&for=us%3A%2A":
                {"error": True},
        })
        with pytest.raises(ValueError, match="shape"):
            cen.query("x", "y", ["a"], geo_for="us:*")

    def test_timeseries_time_predicate(self, monkeypatch):
        monkeypatch.setenv("CALLISTO_CENSUS_API_KEY", "ck")
        cen, t = build("census", "CensusAdapter", {
            "https://api.census.gov/data/timeseries/eits/vip"
            "?get=cell_value&for=us%3A%2A&time=2019-01%2B2019-12&key=ck": [
                ["cell_value"], ["100"]],
        })
        cen.timeseries("eits/vip", ["cell_value"], geo_for="us:*",
                       start="2019-01", end="2019-12")
        assert "time=2019-01" in t.calls[0][0] and "2019-12" in t.calls[0][0]


class TestEia:
    def test_series_requires_key_header_not_query(self, monkeypatch):
        eia, t = build("eia", "EiaAdapter", {
            EIA_BASE + "/seriesid/PET.WCESTUS1.W?frequency=weekly": {
                "response": {"data": [{"period": "2024-01-05",
                                       "value": 73.81}]}}},
        )
        monkeypatch.setenv("CALLISTO_EIA_API_KEY", "ek")
        out = eia.series("pet.wcestus1.w", frequency="weekly")
        assert out["series_id"] == "PET.WCESTUS1.W"
        assert out["data"][0]["value"] == 73.81
        assert t.calls[0][1]["X-Api-Key"] == "ek"
        assert "api_key=ek" not in t.calls[0][0] and "key=ek" not in t.calls[0][0]

    def test_missing_key_no_fetch(self):
        eia, t = build("eia", "EiaAdapter", {})
        with pytest.raises(SourceError, match="CALLISTO_EIA_API_KEY"):
            eia.series("PET.WCESTUS1.W")
        assert t.calls == []


class TestFdic:
    def test_financials_filter_and_rows(self):
        fdic, _t = build("fdic", "FdicAdapter", {
            # host moved to api.fdic.gov (old one 301s) — I2 live smoke
            "https://api.fdic.gov/banks/financials?"
            "filters=CERT%3A3510&field_names=ASSET%2CDEP&limit=40"
            "&sort_by=REPDTE&sort_order=DESC": {
                "data": [{"data": {"ASSET": 1000, "DEP": 800}}]}},
        )
        out = fdic.financials(3510, fields=("ASSET", "DEP"))
        assert out["rows"][0]["ASSET"] == 1000

    def test_filter_injection_rejected(self):
        fdic, t = build("fdic", "FdicAdapter", {})
        with pytest.raises(ValueError, match="filter"):
            fdic.institutions(filters="CERT:1 OR 1=1; DROP")
        assert t.calls == []


class TestCftc:
    BASE = "https://publicreporting.cftc.gov/resource"

    def test_contract_history_socrata_params(self):
        cot, _t = build("cftc", "CftcCotAdapter", {
            self.BASE + "/6dca-aqww.json?%24limit=52&%24where=cftc_contract_"
                        "market_code%3D%27088691%27&%24order=report_date_as_"
                        "yyyy_mm_dd+DESC": [
                {"report_date_as_yyyy_mm_dd": "2024-01-02",
                 "open_interest_all": "1700000"}]},
        )
        out = cot.contract_history("088691", weeks=52)
        assert out["rows"][0]["open_interest_all"] == "1700000"

    def test_unknown_dataset_rejected(self):
        cot, t = build("cftc", "CftcCotAdapter", {})
        with pytest.raises(ValueError, match="dataset"):
            cot.query("bogus-resource")
        assert t.calls == []


class TestWorldBank:
    def test_indicator_normalized_rows(self):
        wb, _t = build("worldbank", "WorldBankAdapter", {
            "https://api.worldbank.org/v2/country/usa/indicator/"
            "NY.GDP.MKTP.CD?format=json&per_page=200&date=2020%3A2023": [
                {"page": 1, "total": 4},
                [{"country": {"value": "United States"}, "date": "2023",
                  "value": 27360000000000},
                 {"country": {"value": "United States"}, "date": "2022",
                  "value": None}]]},
        )
        out = wb.indicator("USA", "NY.GDP.MKTP.CD", start="2020", end="2023")
        assert out["total"] == 4
        assert out["rows"][0]["value"] == 27360000000000
        assert out["rows"][1]["value"] is None      # missing stays missing
        assert "_fetch" in out

    def test_bad_iso_rejected(self):
        wb, t = build("worldbank", "WorldBankAdapter", {})
        with pytest.raises(ValueError, match="ISO3"):
            wb.indicator("usa!", "NY.GDP.MKTP.CD")
        assert t.calls == []


class TestSemanticScholar:
    def test_paper_by_arxiv_id(self, monkeypatch):
        ss, _t = build("semantic_scholar", "SemanticScholarAdapter", {
            S2_BASE + "/paper/arXiv:1706.03762?fields=title%2Cyear%2Cabstract%2CcitationCount%2CinfluentialCitationCount%2CexternalIds%2CopenAccessPdf%2Ctldr":
                {"title": "Attention Is All You Need", "year": 2017}},
        )
        monkeypatch.delenv("CALLISTO_S2_API_KEY", raising=False)
        out = ss.paper("arXiv:1706.03762")
        assert out["title"] == "Attention Is All You Need"


# ── Job 2: Wayback → PublicationProof → CutoffEnforcer ────────────────────

class TestWaybackProofs:
    def _staged(self, body_text="PRICES AS OF MAY 2025"):
        routes = {
            WAYBACK_BASE + "/available?url=https%3A%2F%2Fexample.com%2Fpricing"
                           "&timestamp=20260101235959": {
                "archived_snapshots": {"closest": {
                    "available": True, "status": 200,
                    "timestamp": "20230515000000", "url": SNAP_URL}}},
            SNAP_URL: body_text,
        }
        return build("wayback", "WaybackAdapter", routes)

    def test_snapshot_proof_minted_and_admitted(self, monkeypatch):
        """End-to-end on the SIGNED path: wayback signs, enforcer verifies.

        Both ends resolve the same secret via cutoff.harness_key(). Before W5
        was fixed nothing signed and the enforcer skipped verification, so this
        test passed while the signature system was entirely inert.
        """
        import datetime as dt
        from tools.retrodiction.cutoff import CutoffEnforcer, ProofKind
        monkeypatch.setenv("CALLISTO_CUTOFF_KEY", "harness-secret")

        wb, _t = self._staged()
        before = dt.date(2026, 1, 1)
        rec, reason = wb.evidence_record(
            "https://example.com/pricing", query="what were prices?",
            before=before, fetched_at=dt.datetime(2026, 8, 22))
        assert rec is not None, reason
        assert rec.proof.kind == ProofKind.IMMUTABLE_SNAPSHOT
        assert rec.proof.published_on == dt.date(2023, 5, 15)
        assert rec.proof.locator == SNAP_URL
        assert rec.content.startswith("PRICES AS OF")
        # the real consumer accepts it end-to-end
        admitted, rejected = CutoffEnforcer(before).admit([rec])
        assert len(admitted) == 1 and rejected == []

    def test_signed_proof_passes_signature_check(self):
        import datetime as dt
        from tools.retrodiction.cutoff import CutoffEnforcer

        wb, _t = self._staged()
        before = dt.date(2026, 1, 1)
        rec, _ = wb.evidence_record("https://example.com/pricing",
                                    query="q", before=before,
                                    sign_key="harness-secret")
        admitted, rejected = CutoffEnforcer(
            before, signing_key="harness-secret").admit([rec])
        assert len(admitted) == 1 and rejected == []
        # forged key fails closed
        _, rejected2 = CutoffEnforcer(before, signing_key="wrong").admit([rec])
        assert len(rejected2) == 1

    def test_capture_after_cutoff_yields_no_proof(self):
        import datetime as dt
        wb, _t = build("wayback", "WaybackAdapter", {
            WAYBACK_BASE + "/available?url=https%3A%2F%2Fx.test%2Fa"
                           "&timestamp=20200601235959": {
                "archived_snapshots": {"closest": {
                    "timestamp": "20210615000000",
                    "url": "https://web.archive.org/web/20210615/x"}}},
        })
        proof, reason = wb.snapshot_proof("https://x.test/a",
                                          dt.date(2020, 6, 1))
        assert proof is None
        assert "not strictly before" in reason

    def test_no_snapshot_fails_closed(self):
        import datetime as dt
        wb, _t = build("wayback", "WaybackAdapter", {
            WAYBACK_BASE + "/available?url=https%3A%2F%2Fnobody.test%2F"
                           "&timestamp=20200601235959":
                {"archived_snapshots": {}},
        })
        proof, reason = wb.snapshot_proof("https://nobody.test/",
                                          dt.date(2020, 6, 1))
        assert proof is None
        assert "no wayback snapshot" in reason


# ── Job 3: selection layer ────────────────────────────────────────────────

def _spec(name, answers, tier=1):
    return SourceSpec(name=name, base_url=f"https://{name}.test",
                      description=name, answers=tuple(answers), tier=tier)


class TestSelectionLayer:
    def _registry(self):
        reg = SourceRegistry()
        reg.register(SourceAdapter(
            spec=_spec("gdp_trade",
                       ["GDP and trade balances", "industry accounts"]),
            make_adapter=lambda s: None))
        reg.register(SourceAdapter(
            spec=_spec("partial", ["prices only"], tier=2),
            make_adapter=lambda s: None))
        reg.register(SourceAdapter(
            spec=_spec("unrelated", ["protein structures"]),
            make_adapter=lambda s: None))
        return reg

    def test_every_source_gets_a_decision_included_or_not(self):
        reg = self._registry()
        decisions = reg.select_explained("GDP trade prices")
        assert {d.name for d in decisions} == \
            {a.spec.name for a in reg._adapters.values()}
        by = {d.name: d for d in decisions}
        assert by["gdp_trade"].included
        assert by["unrelated"].included is False
        assert by["unrelated"].reasons          # says WHY it was skipped

    def test_full_coverage_ranks_above_partial_then_tier(self):
        reg = self._registry()
        picks = reg.select("GDP trade prices")
        assert [p.name for p in picks][:1] == ["gdp_trade"]
        d = {d.name: d for d in reg.select_explained("GDP trade prices")}
        assert d["partial"].score < d["gdp_trade"].score

    def test_min_score_threshold_excludes_tangential(self):
        reg = self._registry()
        strict = reg.select_explained("GDP trade prices", min_score=0.99)
        assert not any(d.name == "partial" and d.included for d in strict)
        loose = reg.select_explained("GDP trade prices", min_score=0.0)
        assert any(d.name == "partial" and d.included for d in loose)

    def test_exclude_reason_named(self):
        reg = self._registry()
        d = {d.name: d for d in
             reg.select_explained("GDP", exclude={"gdp_trade"})}
        assert d["gdp_trade"].included is False
        assert "excluded by caller" in d["gdp_trade"].reasons[0]

    def test_tier_ceiling_reason_names_the_ceiling(self):
        reg = self._registry()
        d = {d.name: d for d in reg.select_explained("prices", max_tier=1)}
        assert d["partial"].included is False
        assert "exceeds ceiling 1" in d["partial"].reasons[0]

    def test_select_backcompat_returns_specs_only(self):
        reg = self._registry()
        picks = reg.select("GDP trade prices")
        assert all(isinstance(p, SourceSpec) for p in picks)
        # 'partial' answers only 'prices'. It IS included at default
        # strictness, which is what select_explained's own docstring says
        # should happen: "partial coverage still includes, because a source
        # answering only 'prices' genuinely bears on 'energy prices
        # inventories' even if it cannot answer the rest."
        #
        # This assertion previously read == ["gdp_trade"], pinning a
        # threshold artifact rather than the stated intent: one match across
        # three topical words scores 0.333, a hair under the 0.34 floor. That
        # same cliff meant "patents filed by a company" selected nothing while
        # "patents" selected uspto_odp. Ranking is still by coverage, so the
        # fuller source comes first.
        assert picks[0].name == "gdp_trade"
        assert {p.name for p in picks} >= {"gdp_trade", "partial"}
        picks_loose = reg.select("GDP trade prices", min_score=0.0)
        assert picks_loose[0].name == "gdp_trade"
        assert {p.name for p in picks_loose} >= {"gdp_trade", "partial"}

    def test_decision_to_dict_is_pipeline_safe(self):
        reg = self._registry()
        d = reg.select_explained("GDP")[0]
        payload = d.to_dict()
        assert set(payload) <= {"name", "included", "score", "reasons",
                                "tier"}
        json.dumps(payload)


# ── registration & spec hygiene across ALL sources ────────────────────────

NEW_NAMES = {
    "sec_fulltext", "courtlistener", "uspto_odp", "bea", "census", "eia",
    "fdic", "cftc_cot", "worldbank", "semanticscholar", "wayback",
}


class TestRegistrationAndHygiene:
    def _fresh_registry(self):
        reg = SourceRegistry()
        from tools.sources.adapters import register_all
        register_all(reg)
        return reg

    def test_all_new_sources_registered(self):
        names = set(self._fresh_registry().names())
        assert NEW_NAMES <= names
        assert len(names) >= 19

    def test_every_spec_honest(self):
        for spec in self._fresh_registry().specs():
            assert spec["answers"], spec["name"]
            assert spec["cannot_answer"], f"{spec['name']} overstates coverage"
            assert spec["tier_meaning"] == PROVENANCE_TIERS[spec["tier"]]
            assert spec["min_interval_s"] > 0
            assert spec["terms_url"], spec["name"]

    def test_rate_limits_under_documented_ceilings(self):
        ceilings_rps = {
            "sec_fulltext": 4,
            "courtlistener": 0.34,   # free-tier quota dominates req/s math
            "uspto_odp": 1,
            "bea": 1,
            "census": 2,
            "eia": 2,
            "fdic": 2,
            "cftc_cot": 1,
            "worldbank": 2,
            "semanticscholar": 1,
            "wayback": 1,
        }
        reg = self._fresh_registry()
        for name, ceiling in ceilings_rps.items():
            rps = 1.0 / reg.get(name).spec.min_interval_s
            assert rps <= ceiling + 1e-9, (
                f"{name}: {rps:.2f} req/s exceeds ceiling {ceiling}")

    def test_keyed_sources_declare_key_env(self):
        reg = self._fresh_registry()
        for name in ("fred", "courtlistener", "uspto_odp", "bea", "eia"):
            assert reg.get(name).spec.key_env_var, name


class TestPluginExplain:
    def test_select_tool_explain_includes_decisions(self):
        import asyncio
        from tools.domain_registry import ToolRegistry
        from tools.sources.plugin import register_if_available

        reg = ToolRegistry()
        register_if_available(reg)
        _, result = asyncio.run(reg.dispatch(
            "source_registry_select",
            {"question_type": "energy prices inventories",
             "max_tier": 3, "explain": True}))
        assert result["ok"] is True
        names = {d["name"] for d in result["decisions"]}
        assert "eia" in {s["name"] for s in result["sources"]}
        assert len(result["decisions"]) >= 19   # every registered source
        eia_d = next(d for d in result["decisions"]
                     if d["name"] == "eia")
        assert eia_d["included"] and eia_d["reasons"]
        skipped = [d for d in result["decisions"] if not d["included"]]
        assert all(d["reasons"] for d in skipped)
