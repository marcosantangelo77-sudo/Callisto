"""Source-provenance integrity: error bytes never mint provenance.

R4 follow-ups. Two seams can otherwise let a FAILED fetch enter the
provenance ledger as if it were an observation of the world:

  SEAM A (RestSource): a non-200 response must raise SourceError and its
  raw wire body must never reach the ledger — not even hashed, not even
  as SECONDARY. A 503 JSON/HTML page must never verify a citation.

  SEAM B (IterativeRetriever scratch replay): parallel fan-out records
  RestSource writes into a per-source scratch recorder and replays them
  into the real ledger afterwards. A source whose outcome is FAIL (e.g.
  a BLS HTTP-200 REQUEST_NOT_PROCESSED quota envelope) contributed no
  admissible observation, so its scratch provenance must be dropped.
  The canonical sorted-JSON form the pipeline carries forward differs
  from the raw wire body, so these tests hash the RAW fixture strings,
  not any re-serialized dict.

All fetches run through injected transports under the no-socket guard.
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

from agp.provenance import ProvenanceLedger  # noqa: E402
from agp.research_program import (  # noqa: E402
    EvidenceRequirement,
    QuestionKind,
    ResearchQuestion,
    SourceClassRank,
)
from tools.pipeline.retrieval import IterativeRetriever, RelevanceGate  # noqa: E402
from tools.sources.base import (  # noqa: E402
    RestSource,
    SourceError,
    SourceSpec,
    _RateLimiter,
)
from tools.sources.registry import SourceAdapter, SourceRegistry  # noqa: E402

MACRO_SPEC = SourceSpec(
    name="fake_macro", base_url="https://api.example.test/fred",
    description="fixture macro source", answers=("macro time series",),
    tier=1, key_env_var="CALLISTO_TEST_KEY",
)

URL = "https://api.example.test/fred/x"

JSON_503 = ('{"error": "upstream capacity exceeded",'
            ' "hint": "retry later", "code": 503}')
HTML_503 = ("<html><body><h1>503 Service Unavailable</h1>"
            "<p>No server is available for this request.</p></body></html>")


def _make(spec=MACRO_SPEC, ledger=None, status=200, body="{}"):
    def transport(url, headers):
        return status, body
    return RestSource(spec, ledger=ledger, transport=transport,
                      _limiter=_RateLimiter(0.0))


def _no_sleep(monkeypatch):
    """503 retries back off exponentially; tests must not actually wait."""
    import tools.sources.base as base
    monkeypatch.setattr(base.time, "sleep", lambda s: None)


# ── SEAM A: non-200 wire bodies never enter the ledger ────────────────────

class TestNon200NeverProvenanced:
    def test_get_503_json_raw_body_absent_from_ledger(self, monkeypatch):
        _no_sleep(monkeypatch)
        ledger = ProvenanceLedger()
        src = _make(ledger=ledger, status=503, body=JSON_503)
        with pytest.raises(SourceError, match="503"):
            src.get_json(URL)
        # Exact RAW wire bytes (not canonicalized JSON) are absent.
        assert not ledger.has_observation(JSON_503)
        assert not ledger.is_primary_bytes(JSON_503)
        assert URL not in ledger.observed_urls()

    def test_get_503_html_raw_body_absent_from_ledger(self, monkeypatch):
        _no_sleep(monkeypatch)
        ledger = ProvenanceLedger()
        src = _make(ledger=ledger, status=503, body=HTML_503)
        with pytest.raises(SourceError, match="503"):
            src.get_json(URL)
        assert not ledger.has_observation(HTML_503)
        assert not ledger.is_primary_bytes(HTML_503)
        assert URL not in ledger.observed_urls()

    def test_post_503_json_raw_body_absent_from_ledger(self, monkeypatch):
        _no_sleep(monkeypatch)
        ledger = ProvenanceLedger()
        src = _make(ledger=ledger, status=503, body=JSON_503)
        with pytest.raises(SourceError, match="503"):
            src.post_json(URL, {"seriesid": ["LNS14000000"]})
        assert not ledger.has_observation(JSON_503)
        assert not ledger.is_primary_bytes(JSON_503)
        assert URL not in ledger.observed_urls()

    def test_post_503_html_raw_body_absent_from_ledger(self, monkeypatch):
        _no_sleep(monkeypatch)
        ledger = ProvenanceLedger()
        src = _make(ledger=ledger, status=503, body=HTML_503)
        with pytest.raises(SourceError, match="503"):
            src.post_json(URL, {"seriesid": ["LNS14000000"]})
        assert not ledger.has_observation(HTML_503)
        assert not ledger.is_primary_bytes(HTML_503)
        assert URL not in ledger.observed_urls()


# ── SEAM B: failed sources' scratch provenance is never replayed ──────────

def _registry(*specs) -> SourceRegistry:
    """specs: (name, answers, base_url) triples."""
    reg = SourceRegistry()

    def make_adapter(source):
        path = "/bls"

        class _Ad:
            def __getattr__(self, method_name):
                def call(*args, **kwargs):
                    term = next((a for a in args if isinstance(a, str)),
                                kwargs.get("query_term", "q"))
                    url = source.build_url(
                        path, {"search": str(term).replace(" ", "+")})
                    return source.get_json(url)[0]
                return call
        return _Ad()

    for name, answers, base_url in specs:
        spec = SourceSpec(
            name=name, base_url=base_url, description="",
            answers=tuple(answers), cannot_answer=("x",), tier=1,
            min_interval_s=0.0)
        reg.register(SourceAdapter(spec=spec, make_adapter=make_adapter))
    return reg


def _retriever(reg, routes):
    ledger = ProvenanceLedger()
    return IterativeRetriever(
        registry=reg, ledger=ledger,
        transport=_routes_transport(routes),
        gate=RelevanceGate(min_coverage=0.25),
        max_rounds=2,
        generic_calls={
            "bls": ("works_search", ("term",), {"limit": 3}),
            "beta": ("works_search", ("term",), {"limit": 3}),
        }), ledger


def _routes_transport(routes: dict[str, tuple[int, str]]):
    """Serve canned (status, raw_body) by URL substring. Unlike the
    engine's fixture_transport this can stage non-200 statuses."""
    def transport(url, headers):
        for pattern, (status, body) in routes.items():
            if pattern in url:
                return status, body
        return 404, '{"error": "no fixture route"}'
    return transport


def _q(min_ind=1):
    rq = ResearchQuestion(
        text="What does the unemployment rate show about the labor market?",
        kind=QuestionKind.DESCRIPTIVE)
    rq.evidence_requirements = EvidenceRequirement(
        min_source_class=SourceClassRank.SECONDARY,
        min_independent_sources=min_ind)
    return rq


# Raw non-canonical BLS envelope: whitespace + key order deliberately
# differ from any json.dumps(sort_keys=True) re-serialization, so a test
# that only checked the canonicalized form could pass while the RAW wire
# bytes leaked into the ledger.
BLS_QUOTA_RAW = ('{ "message": [ "daily threshold reached" ],\n'
                 '   "status":[ "REQUEST_NOT_PROCESSED" ] }\n')

GOOD_BODY = json.dumps({"results": [
    {"id": "W1", "title": "Unemployment rate labor market review",
     "publication_year": 2024}]})


class TestFailOutcomeScratchNotReplayed:
    def test_bls_200_quota_envelope_raw_bytes_never_in_ledger(self):
        reg = _registry(("bls", ["labor market data"],
                         "https://api.bls.gov"))
        routes = {"/bls?search=": (200, BLS_QUOTA_RAW)}
        retr, ledger = _retriever(reg, routes)
        trace = retr.retrieve(_q(), "", min_independent=1)

        # The envelope was classified as a fetch failure and reported.
        assert any(r["name"] == "bls" and "error" in r
                   for rnd in trace.rounds for r in rnd["sources"])
        # Its RAW wire body — not the canonicalized form — must be absent
        # from ledger provenance.
        assert not ledger.has_observation(BLS_QUOTA_RAW)
        assert not ledger.is_primary_bytes(BLS_QUOTA_RAW)
        canonical = json.dumps(json.loads(BLS_QUOTA_RAW), sort_keys=True)
        assert not ledger.has_observation(canonical)
        assert all("api.bls.gov" not in u for u in ledger.observed_urls())
        assert trace.n_admitted == 0

    def test_error_envelope_reported_as_error_not_rejection(self):
        """Trace error reporting is preserved: the failure surfaces as an
        honest source error, distinct from a gate rejection."""
        reg = _registry(("bls", ["labor market data"],
                         "https://api.bls.gov"))
        retr, _ledger = _retriever(reg, {"/bls?search=": (200, BLS_QUOTA_RAW)})
        trace = retr.retrieve(_q(), "", min_independent=1)
        errs = [r.get("error") or "" for rnd in trace.rounds
                for r in rnd["sources"] if r.get("name") == "bls"]
        assert any("quota" in e.lower() for e in errs), errs
        assert trace.rejected == []

    def test_failed_source_does_not_poison_later_good_source(self):
        """A fail outcome skips ONLY its own replay; a succeeding source's
        provenance still lands normally."""
        reg = _registry(("bls", ["labor market data"],
                         "https://api.bls.gov"),
                        ("beta", ["unemployment rate labor market review"],
                         "https://b.org"))
        retr, ledger = _retriever(reg, {
            "/api.bls.gov/bls?": (200, BLS_QUOTA_RAW),
            "/b.org/bls?": (200, GOOD_BODY)})
        trace = retr.retrieve(_q(), "", min_independent=1)

        assert trace.n_admitted >= 1
        assert ledger.has_observation(GOOD_BODY)
        assert ledger.is_primary_bytes(GOOD_BODY)
        assert any(u.startswith("https://b.org") for u in ledger.observed_urls())
        assert not ledger.has_observation(BLS_QUOTA_RAW)


# ── Valid envelopes stay valid; good bodies stay primary/citable ──────────

class TestValidEnvelopesUnchanged:
    def test_bls_request_succeeded_empty_series_is_not_an_error(self):
        from tools.sources import query_builder as qb
        body = {"status": ["REQUEST_SUCCEEDED"], "Results": {"series": []}}
        assert qb.classify_fetch_failure("bls", body) is None

    def test_normal_nonempty_200_stays_primary_and_citation_verifiable(self):
        ledger = ProvenanceLedger()
        src = _make(ledger=ledger, status=200, body=GOOD_BODY)
        src.get_json(URL)
        assert ledger.is_primary_bytes(GOOD_BODY)
        assert URL in ledger.observed_urls()
        assert ledger.cites_verified_url(f"cites {URL} for the claim")


# ── SEAM C: a 200 fetch the ledger cannot record must fail closed ─────────

class _ExplodingLedger:
    """Ledger whose record_tool_result always raises."""

    def record_tool_result(self, *args, **kwargs):
        raise RuntimeError("ledger disk sealed")


class TestUnrecorded200FailsClosed:
    def test_get_json_200_with_failing_ledger_raises_source_error(self):
        src = _make(ledger=_ExplodingLedger(), status=200,
                    body='{"ok": true}')
        with pytest.raises(SourceError) as ei:
            src.get_json(URL)
        msg = str(ei.value)
        assert MACRO_SPEC.name in msg
        assert URL in msg
        # The unverified body must not be handed to the caller.
        assert src.last_record is not None
        assert getattr(src.last_record, "url", None) == URL

    def test_post_json_200_with_failing_ledger_raises_source_error(self):
        src = _make(ledger=_ExplodingLedger(), status=200,
                    body='{"ok": true}')
        with pytest.raises(SourceError) as ei:
            src.post_json(URL, {"q": "x"})
        msg = str(ei.value)
        assert MACRO_SPEC.name in msg
        assert URL in msg

    def test_ledger_failure_chains_original_exception(self):
        src = _make(ledger=_ExplodingLedger(), status=200, body="{}")
        with pytest.raises(SourceError) as ei:
            src.get(URL)
        assert isinstance(ei.value.__cause__, RuntimeError)

    def test_ledger_none_still_returns_200_body(self):
        src = _make(ledger=None, status=200, body='{"ok": true}')
        data, rec = src.get_json(URL)
        assert data == {"ok": True}
        assert rec.url == URL
