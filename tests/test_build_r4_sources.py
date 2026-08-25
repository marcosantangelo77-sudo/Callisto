"""R4 source registry tests.

HARD RULE: no test in this file may open a network socket. The guard
(tests/helpers/no_socket.py) patches socket.socket to raise on
INET sockets; if any code path under test tries a real connection the
suite FAILS. All fetches run through RestSource's injectable transport
with canned fixture payloads.
"""

from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Install the no-socket barrier BEFORE importing anything that could fetch.
from tests.helpers.no_socket import NoSocket  # noqa: E402

_guard = NoSocket()
_guard.install()

import pytest  # noqa: E402

from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.sources.base import (  # noqa: E402
    MAX_RETRIES,
    PROVENANCE_TIERS,
    RestSource,
    SourceError,
    SourceSpec,
    _RateLimiter,
)
from tools.sources.registry import SourceAdapter, SourceRegistry  # noqa: E402


JSON_403 = '{"error": "forbidden by WAF rule", "code": 403}'
JSON_503 = ('{"error": "upstream capacity exceeded",'
            ' "hint": "retry later", "code": 503}')
HTML_503 = ("<html><body><h1>503 Service Unavailable</h1>"
            "<p>No server is available for this request.</p></body></html>")


class FakeTransport:
    """Canned responses; counts calls; raises if a URL was not staged."""

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


def make_source(spec, transport, ledger=None):
    return RestSource(spec, ledger=ledger, transport=transport,
                      _limiter=_RateLimiter(0.0))


MACRO_SPEC = SourceSpec(
    name="fake_macro", base_url="https://api.example.test/fred",
    description="fixture macro source", answers=("macro time series",),
    tier=1, key_env_var="CALLISTO_TEST_KEY",
)


# ── base client ───────────────────────────────────────────────────────────

class TestRestSource:
    def test_get_json_returns_payload_and_record(self):
        t = FakeTransport()
        t.stage("https://api.example.test/fred/x", {"a": 1})
        src = make_source(MACRO_SPEC, t)
        data, rec = src.get_json("https://api.example.test/fred/x")
        assert data["a"] == 1
        assert rec.status == 200
        assert rec.content_sha256

    def test_provenance_ledger_records_fetch(self):
        t = FakeTransport()
        url = "https://api.example.test/fred/x"
        body_dict = {"observations": [1, 2, 3]}
        t.stage(url, body_dict)
        ledger = ProvenanceLedger()
        src = make_source(MACRO_SPEC, t, ledger=ledger)
        src.get_json(url)
        wire_body = t.routes[url]
        assert ledger.is_primary_bytes(wire_body)
        assert url in ledger.observed_urls()

    def test_user_agent_always_sent(self):
        t = FakeTransport()
        t.stage("https://api.example.test/fred/x", {})
        make_source(MACRO_SPEC, t).get_json("https://api.example.test/fred/x")
        _, headers = t.calls[0]
        assert "Callisto" in headers["User-Agent"]

    def test_spec_headers_forwarded_with_key_substitution(self):
        spec = SourceSpec(
            name="h", base_url="https://x.test", description="",
            headers=(("X-Api-Key", "{api_key}"),),
            key_env_var="CALLISTO_TEST_HEADER_KEY", tier=1)
        t = FakeTransport()
        t.stage("https://x.test/a", {})
        os.environ["CALLISTO_TEST_HEADER_KEY"] = "sekrit"
        try:
            make_source(spec, t).get_json("https://x.test/a")
        finally:
            del os.environ["CALLISTO_TEST_HEADER_KEY"]
        _, headers = t.calls[0]
        assert headers["X-Api-Key"] == "sekrit"

    def test_missing_key_env_is_empty(self):
        assert make_source(MACRO_SPEC, FakeTransport()).api_key() == ""

    def _no_sleep(self, monkeypatch):
        import tools.sources.base as base
        monkeypatch.setattr(base.time, "sleep", lambda s: None)

    def test_get_injected_transport_retries_transient_503(self, monkeypatch):
        """The transport seam must preserve native GET retry semantics:
        a transient 503 from the injected transport is retried, not fatal,
        and the eventual 200 body is the only thing provenanced."""
        self._no_sleep(monkeypatch)

        class FlakyTransport:
            def __init__(self):
                self.calls = 0
                self.bodies = []

            def __call__(self, url, headers):
                self.calls += 1
                if self.calls == 1:
                    return 503, HTML_503
                return 200, json.dumps({"a": 1})

        t = FlakyTransport()
        ledger = ProvenanceLedger()
        src = make_source(MACRO_SPEC, t, ledger=ledger)
        data, rec = src.get_json("https://api.example.test/fred/x")

        assert t.calls == 2                      # retried exactly once
        assert data == {"a": 1} and rec.status == 200
        # The error body never minted provenance.
        assert not ledger.has_observation(HTML_503)
        assert ledger.is_primary_bytes(json.dumps({"a": 1}))
        assert "https://api.example.test/fred/x" in ledger.observed_urls()

    def test_get_injected_transport_403_retried_then_200(self, monkeypatch):
        """Parity with the native HTTPError GET path: 403 from the injected
        transport is retried (bounded exponential fallback — this seam has
        no headers), not treated as terminal. The 403 error body never
        enters the provenance ledger; only the eventual 200 body does."""
        self._no_sleep(monkeypatch)

        class ForbiddenThenOk:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, headers):
                self.calls += 1
                if self.calls == 1:
                    return 403, JSON_403
                return 200, json.dumps({"a": 1})

        t = ForbiddenThenOk()
        ledger = ProvenanceLedger()
        src = make_source(MACRO_SPEC, t, ledger=ledger)
        data, rec = src.get_json("https://api.example.test/fred/x")

        assert t.calls == 2                      # retried exactly once
        assert data == {"a": 1} and rec.status == 200
        # The 403 error body never minted provenance.
        assert not ledger.has_observation(JSON_403)
        assert ledger.is_primary_bytes(json.dumps({"a": 1}))

    def test_get_injected_transport_429_retried_then_exhausts(self, monkeypatch):
        """Persistent transient failures exhaust retries into SourceError."""
        self._no_sleep(monkeypatch)

        class Always429:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, headers):
                self.calls += 1
                return 429, '{"error": "rate limited"}'

        t = Always429()
        with pytest.raises(SourceError, match="exhausted retries"):
            make_source(MACRO_SPEC, t).get_json(
                "https://api.example.test/fred/x")
        assert t.calls == MAX_RETRIES

    def test_get_injected_terminal_status_is_not_retried(self, monkeypatch):
        """A non-transient status (e.g. 404) fails immediately."""
        self._no_sleep(monkeypatch)

        class NotFound:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, headers):
                self.calls += 1
                return 404, '{"error": "not found"}'

        t = NotFound()
        with pytest.raises(SourceError, match="404"):
            make_source(MACRO_SPEC, t).get_json(
                "https://api.example.test/fred/x")
        assert t.calls == 1

    def test_post_injected_transport_retries_transient_503(self, monkeypatch):
        """POST's tuple-status path already retries transients; pin it so the
        two seams cannot silently drift apart again."""
        self._no_sleep(monkeypatch)

        class FlakyTransport:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, headers):
                self.calls += 1
                if self.calls == 1:
                    return 503, JSON_503
                return 200, json.dumps({"ok": True})

        t = FlakyTransport()
        ledger = ProvenanceLedger()
        src = make_source(MACRO_SPEC, t, ledger=ledger)
        data, rec = src.post_json("https://api.example.test/fred/x",
                                  {"seriesid": ["LNS14000000"]})

        assert t.calls == 2
        assert data == {"ok": True} and rec.status == 200
        assert not ledger.has_observation(JSON_503)

    def test_post_injected_transport_403_retried_then_200(self, monkeypatch):
        """Parity with GET: a transient POST 403 (temporary WAF/edge block,
        as BLS/USPTO can produce) is retried with the bounded policy; only
        the eventual 200 body enters the provenance ledger."""
        self._no_sleep(monkeypatch)

        class ForbiddenThenOk:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, headers):
                self.calls += 1
                if self.calls == 1:
                    return 403, JSON_403
                return 200, json.dumps({"ok": True})

        t = ForbiddenThenOk()
        ledger = ProvenanceLedger()
        src = make_source(MACRO_SPEC, t, ledger=ledger)
        data, rec = src.post_json("https://api.example.test/fred/x",
                                  {"seriesid": ["LNS14000000"]})

        assert t.calls == 2                      # retried exactly once
        assert data == {"ok": True} and rec.status == 200
        # The 403 error body never minted provenance.
        assert not ledger.has_observation(JSON_403)
        assert ledger.is_primary_bytes(json.dumps({"ok": True}))

    def test_post_injected_transport_403_exhausts_retries(self, monkeypatch):
        """Persistent POST 403 performs exactly MAX_RETRIES attempts then
        raises SourceError."""
        self._no_sleep(monkeypatch)

        class Always403:
            def __init__(self):
                self.calls = 0

            def __call__(self, url, headers):
                self.calls += 1
                return 403, JSON_403

        t = Always403()
        with pytest.raises(SourceError, match="exhausted retries"):
            make_source(MACRO_SPEC, t).post_json(
                "https://api.example.test/fred/x", {"a": 1})
        assert t.calls == MAX_RETRIES

    def test_post_native_httperror_403_retried_then_200(self, monkeypatch):
        """Parity with the native GET HTTPError path: urllib raising
        HTTPError(403) once then succeeding is retried under the same
        bounded policy."""
        import tools.sources.base as base

        self._no_sleep(monkeypatch)

        class FlakyUrlopen:
            def __init__(self):
                self.calls = 0

            def __call__(self, req, timeout=None, context=None):
                self.calls += 1
                if self.calls == 1:
                    raise base.urllib.error.HTTPError(
                        req.full_url, 403, "Forbidden",
                        hdrs={"Retry-After": "0"}, fp=None)
                resp = FakeHTTPResponse(json.dumps({"ok": True}).encode())
                return resp

        opener = FlakyUrlopen()
        monkeypatch.setattr(base.urllib.request, "urlopen", opener)
        ledger = ProvenanceLedger()
        src = make_source(MACRO_SPEC, None, ledger=ledger)
        data, rec = src.post_json("https://api.example.test/bls/timeseries",
                                  {"seriesid": ["LNS14000000"]})

        assert opener.calls == 2                 # retried exactly once
        assert data == {"ok": True} and rec.status == 200
        assert not ledger.has_observation(JSON_403)

    def test_post_native_httperror_403_no_headers_retried(self, monkeypatch):
        """HTTPError with hdrs=None must follow the same bounded transient-403
        policy instead of raising AttributeError on the first attempt."""
        import tools.sources.base as base

        self._no_sleep(monkeypatch)

        class Headerless403:
            def __init__(self):
                self.calls = 0

            def __call__(self, req, timeout=None, context=None):
                self.calls += 1
                if self.calls <= MAX_RETRIES - 1:
                    raise base.urllib.error.HTTPError(
                        req.full_url, 403, "Forbidden", hdrs=None, fp=None)
                resp = FakeHTTPResponse(json.dumps({"ok": True}).encode())
                return resp

        opener = Headerless403()
        monkeypatch.setattr(base.urllib.request, "urlopen", opener)
        ledger = ProvenanceLedger()
        src = make_source(MACRO_SPEC, None, ledger=ledger)
        data, rec = src.post_json("https://api.example.test/bls/timeseries",
                                  {"seriesid": ["LNS14000000"]})

        assert opener.calls == MAX_RETRIES - 1 + 1
        assert data == {"ok": True} and rec.status == 200
        assert not ledger.has_observation(JSON_403)

    def test_post_native_httperror_403_no_headers_exhausts(self, monkeypatch):
        import tools.sources.base as base

        self._no_sleep(monkeypatch)

        class Always403NoHeaders:
            def __init__(self):
                self.calls = 0

            def __call__(self, req, timeout=None, context=None):
                self.calls += 1
                raise base.urllib.error.HTTPError(
                    req.full_url, 403, "Forbidden", hdrs=None, fp=None)

        opener = Always403NoHeaders()
        monkeypatch.setattr(base.urllib.request, "urlopen", opener)
        ledger = ProvenanceLedger()
        src = make_source(MACRO_SPEC, None, ledger=ledger)
        with pytest.raises(SourceError, match="exhausted retries"):
            src.post_json("https://api.example.test/bls/timeseries",
                          {"seriesid": ["LNS14000000"]})
        assert opener.calls == MAX_RETRIES
        assert not ledger.has_observation(JSON_403)


class FakeHTTPResponse:
    """Minimal context-manager response for native urlopen mocking."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def status(self):
        return 200

    def read(self):
        return self._body


# ── adapters against fixtures ─────────────────────────────────────────────

def build(module_name, cls_name, routes):
    import importlib

    mod = importlib.import_module(f"tools.sources.{module_name}")
    cls = getattr(mod, cls_name)
    t = FakeTransport()
    for url, payload in routes.items():
        t.stage(url, payload)
    return cls(make_source(mod.SPEC, t)), t


class TestAdaptersOnFixtures:
    def test_fred_observations(self):
        fred, _t = build("fred", "FredAdapter", {
            "https://api.stlouisfed.org/fred/series/observations"
            "?series_id=CPIAUCSL&api_key=k&file_type=json":
                {"observations": [{"date": "2024-01-01", "value": "308.417"}]},
        })
        fred.source.api_key = lambda: "k"
        out = fred.series_observations("CPIAUCSL")
        assert out["observations"][0]["value"] == "308.417"
        assert "_fetch" in out
        # api_key must be in the query string, never the UA or headers
        assert "api_key=k" in _t.calls[0][0]

    def test_openalex_works_search(self):
        oa, _t = build("openalex", "OpenAlexAdapter", {
            "https://api.openalex.org/works?search=mRNA&per-page=5":
                {"results": [{"id": "W1", "title": "mRNA"}]},
        })
        out = oa.works_search("mRNA", limit=5)
        assert out["results"][0]["id"] == "W1"

    def test_clinicaltrials_get_study_normalizes_case(self):
        ct, _t = build("clinicaltrials", "ClinicalTrialsAdapter", {
            "https://clinicaltrials.gov/api/v2/studies/NCT12345678":
                {"protocolSection": {"identificationModule":
                                         {"nctId": "NCT12345678"}}},
        })
        out = ct.get_study("nct12345678")
        assert out["protocolSection"]["identificationModule"]["nctId"] \
            == "NCT12345678"

    def test_clinicaltrials_rejects_non_nct(self):
        ct, _t = build("clinicaltrials", "ClinicalTrialsAdapter", {})
        with pytest.raises(ValueError):
            ct.get_study("ISRCTN001")

    def test_treasury_query(self):
        tr, _t = build("treasury", "TreasuryAdapter", {
            "https://api.fiscaldata.treasury.gov/services/api/"
            "fiscal_service/v2/accounting/od/avg_interest_rates"
            "?limit=10&sort=-record_date":
                {"data": [{"record_date": "2024-01-31"}]},
        })
        out = tr.query("v2/accounting/od/avg_interest_rates", limit=10)
        assert out["data"][0]["record_date"] == "2024-01-31"

    def test_bls_caps_enforced_in_code(self):
        bls, _t = build("bls", "BlsAdapter", {})
        # Over the no-key series cap → rejected before any fetch attempt.
        with pytest.raises(SourceError, match="caps"):
            bls.timeseries(["LNS14000000"] * 26, 2020, 2023)

    def test_bls_year_span_cap(self):
        bls, _t = build("bls", "BlsAdapter", {})
        with pytest.raises(SourceError, match="history"):
            bls.timeseries(["LNS14000000"], 2000, 2023)

    def test_wikidata_sparql(self):
        wd, t = build("wikidata", "WikidataAdapter", {})
        q = "SELECT ?w WHERE { ?w wdt:P31 wd:Q5 } LIMIT 1"
        from urllib.parse import urlencode

        url = ("https://query.wikidata.org/sparql?"
               + urlencode({"query": q, "format": "json"}))
        t.stage(url, {"results": {"bindings": []}})
        assert "bindings" in wd.sparql(q)["results"]

    def test_gdelt_timeline(self):
        gd, _t = build("gdelt", "GdeltAdapter", {
            "https://api.gdeltproject.org/api/v2/doc/doc"
            "?query=semiconductors&mode=timelinevol&format=json"
            "&maxrecords=75&timespan=1w":
                {"timeline": [{"series": "Volume Intensity"}]},
        })
        out = gd.coverage_timeline("semiconductors")
        assert out["timeline"][0]["series"] == "Volume Intensity"


# ── registry / selection ──────────────────────────────────────────────────

def registry_with(*pairs):
    reg = SourceRegistry()
    for m, c in pairs:
        mod = __import__(f"tools.sources.{m}", fromlist=[c])
        reg.register(SourceAdapter(spec=mod.SPEC, make_adapter=getattr(mod, c)))
    return reg


class TestRegistry:
    def test_select_orders_lowest_tier_first_and_filters_by_ceiling(self):
        reg = registry_with(("fred", "FredAdapter"),
                            ("openalex", "OpenAlexAdapter"),
                            ("gdelt", "GdeltAdapter"))
        picks = reg.select("macro time series")
        assert picks[0].name == "fred"          # tier 1 before tier 2
        assert all(p.tier <= 5 for p in picks)
        # ceiling excludes tier-4 GDELT even though it's registered
        assert "gdelt" not in {p.name for p in reg.select("news coverage", max_tier=2)}
        assert "gdelt" in {p.name for p in reg.select("news coverage", max_tier=4)}

    def test_select_exclude_drops_failed_sources(self):
        reg = registry_with(("fred", "FredAdapter"))
        assert reg.select("macro time series") != []
        assert reg.select("macro time series", exclude={"fred"}) == []

    def test_every_registered_spec_declares_limits(self):
        from tools.sources.adapters import register_all

        reg = SourceRegistry()
        register_all(reg)
        names = set(reg.names())
        assert {"fred", "openalex", "clinicaltrials", "federalregister",
                "treasury", "bls", "wikidata", "gdelt"} <= names
        for spec in reg.specs():
            assert spec["answers"], f"{spec['name']} declares no capabilities"
            assert spec["cannot_answer"], (
                f"{spec['name']} overstates coverage: empty cannot_answer")
            assert spec["tier_meaning"] == PROVENANCE_TIERS[spec["tier"]]
            assert spec["min_interval_s"] > 0
            assert spec["terms_url"], f"{spec['name']} has no terms link"

    def test_rate_limits_under_stated_ceilings(self):
        """Each adapter's self-imposed interval must sit under its source's
        documented req/s ceiling (values from each source's own docs)."""
        ceilings_rps = {
            "fred": 2,             # docs ask ≤120/min; we target ~2/s
            "openalex": 5,         # polite pool allows up to 100k/day ≈ 10/s; halve it
            "clinicaltrials": 2,   # stated ≤10/s; we take a fifth
            "federalregister": 2,  # no hard limit; be polite
            "treasury": 1,         # stated 1/s sustained — exactly at it
            "bls": 1,              # conservative under 500/day keyed
            "wikidata": 0.5,       # one query at a time, gentle
            "gdelt": 0.2,          # aggressive scraping gets blocked
        }
        from tools.sources.adapters import register_all

        reg = SourceRegistry()
        register_all(reg)
        for name, ceiling in ceilings_rps.items():
            rps = 1.0 / reg.get(name).spec.min_interval_s
            assert rps <= ceiling + 1e-9, (
                f"{name}: {rps:.2f} req/s exceeds documented ceiling {ceiling}")


# ── DomainPlugin wiring ───────────────────────────────────────────────────

class TestPlugin:
    def test_plugin_registers_and_dispatches_list(self):
        import asyncio

        from tools.domain_registry import ToolRegistry
        from tools.sources.plugin import register_if_available

        reg = ToolRegistry()
        assert register_if_available(reg) is True
        tools = reg.tool_names_for(None, "which source covers CPI?")
        assert "source_registry_list" in tools
        handled, result = asyncio.run(reg.dispatch("source_registry_list", {}))
        assert handled and result["ok"] is True
        assert result["count"] >= 8

    def test_select_tool_reports_honest_gap(self):
        import asyncio

        from tools.domain_registry import ToolRegistry
        from tools.sources.plugin import register_if_available

        reg = ToolRegistry()
        register_if_available(reg)
        _, result = asyncio.run(reg.dispatch(
            "source_registry_select",
            {"question_type": "live equity quotes", "max_tier": 3}))
        assert result["ok"] is True
        assert result["sources"] == []  # honest: nothing registered does this
        assert "do NOT fall back" in result["note"]


# ── shared per-identity rate limiter ──────────────────────────────────────


class TestSharedRateLimiter:
    """Default RestSource instances for one stable source identity must share
    a single _RateLimiter process-wide, with no sockets and no real sleeps."""

    @pytest.fixture(autouse=True)
    def _isolate(self):
        from tools.sources import base as base_mod

        base_mod.reset_shared_rate_limiters()
        yield
        base_mod.reset_shared_rate_limiters()

    @pytest.fixture()
    def clock(self, monkeypatch):
        """Deterministic fake monotonic clock + sleep recorder."""
        from tools.sources import base as base_mod

        state = {"now": 1000.0}

        def fake_monotonic():
            return state["now"]

        slept: list[float] = []

        def fake_sleep(d):
            assert d <= 1.0 + 1e-9  # bounded chunking preserved
            state["now"] += d
            slept.append(d)

        monkeypatch.setattr(base_mod.time, "monotonic", fake_monotonic)
        monkeypatch.setattr(base_mod.time, "sleep", fake_sleep)
        return state, slept

    def _spec(self, name="shared_fixture_src"):
        return SourceSpec(
            name=name,
            base_url=f"https://{name}.test/api",
            description="shared-limiter fixture",
            answers=("macro time series",),
            tier=1,
            min_interval_s=0.5,
        )

    def test_cross_instance_spacing(self, clock):
        state, slept = clock
        url_a = "https://shared_fixture_src.test/api/a"
        url_b = "https://shared_fixture_src.test/api/b"
        t = FakeTransport()
        t.stage(url_a, {})
        t.stage(url_b, {})
        a = RestSource(self._spec(), transport=t)
        b = RestSource(self._spec(), transport=t)

        # Same limiter object is shared across independently built instances.
        assert a._limiter is b._limiter

        a.get_json(url_a)
        state["now"] += 0.0  # immediate second request from another instance
        b.get_json(url_b)
        # Instance B was delayed by the declared interval (one bounded sleep).
        assert sum(slept) == pytest.approx(0.5)
        assert len(t.calls) == 2

    def test_distinct_identities_do_not_share(self, clock):
        state, slept = clock
        s1 = self._spec("shared_fixture_src")
        s2 = self._spec("shared_fixture_other")
        t = FakeTransport()
        t.stage("https://shared_fixture_src.test/api/x", {})
        t.stage("https://shared_fixture_other.test/api/x", {})
        a = RestSource(s1, transport=t)
        b = RestSource(s2, transport=t)
        assert a._limiter is not b._limiter
        a.get_json("https://shared_fixture_src.test/api/x")
        b.get_json("https://shared_fixture_other.test/api/x")
        assert slept == []  # no cross-identity delay

    def test_interval_change_is_part_of_identity(self, clock):
        from tools.sources.base import _shared_rate_limiter

        fast = SourceSpec(name="shared_fixture_src", base_url="https://s.test",
                          description="", min_interval_s=0.1)
        slow = SourceSpec(name="shared_fixture_src", base_url="https://s.test",
                          description="", min_interval_s=0.9)
        assert _shared_rate_limiter(fast) is not _shared_rate_limiter(slow)

    def test_explicit_limiter_not_replaced_by_cache(self, clock):
        custom = _RateLimiter(0.25)
        src = RestSource(self._spec(), transport=FakeTransport(),
                         _limiter=custom)
        assert src._limiter is custom
        # and the cache did not grow with the override
        from tools.sources import base as base_mod
        assert all(l is not custom for l in base_mod._shared_limiters.values())
