"""Tier 3 epistemics — provenance-assigned source class + verified citations.

Implements findings/instance4.md mechanism 1 (source class from the code
path that produced the evidence, not from the model's JSON) and mechanism 2
(a citation counts only when it names a URL the session actually fetched).
These are the new behavior tests; the orchestrator adoption diff is a
PROPOSAL (orchestrator.py read-only for this instance).
"""
import pytest

from agp import Domain, Evidence, SourceClass
from agp.provenance import (
    ProvenanceLedger,
    clamp_confidence_provenance,
    extract_urls,
    relabel_evidence,
)

MAX_BY = {"PRIMARY": 1.0, "SECONDARY": 0.75, "SIGNAL": 0.55, "INFERRED": 0.55}


def _ev(content, declared=SourceClass.SECONDARY, conf=0.75):
    return Evidence(content=content, source_class=declared, confidence_score=conf,
                    domain=Domain.GENERAL, origin_agent="model")


class TestExtractUrls:
    def test_basic_and_literal_http(self):
        assert extract_urls("see https://a.example.com/x?q=1 now") == {"https://a.example.com/x?q=1"}
        # 'http://' alone is not a URL — no netloc.
        assert extract_urls("the string http:// appears here") == set()
        assert extract_urls("") == set()

    def test_trailing_punctuation_stripped(self):
        assert extract_urls("at https://x.io/a.") == {"https://x.io/a"}


class TestCitationLoopholeClosed:
    """The old predicate was ('http://' in lowered). Fabricated URLs bought
    +0.20 ceiling. The ledger-based predicate requires an observed fetch."""

    def test_fabricated_url_does_not_count(self):
        led = ProvenanceLedger()
        assert not led.cites_verified_url("see https://totally-fabricated.example.net/x")

    def test_literal_http_string_does_not_count(self):
        led = ProvenanceLedger()
        assert not led.cites_verified_url("the string http:// appears here")

    def test_observed_url_counts(self):
        led = ProvenanceLedger()
        led.record_tool_result("web_fetch", "body", urls=["https://real.example.com/doc"])
        assert led.cites_verified_url("per https://real.example.com/doc the answer is X")

    def test_substring_url_is_not_enough_without_fetch(self):
        led = ProvenanceLedger()
        led.record_tool_result("web_search", "results json", urls=["https://a.example/1"])
        assert led.cites_verified_url("https://a.example/1 says Y")
        assert not led.cites_verified_url("https://b.example/2 says Y")


class TestProvenanceAssignment:
    def test_fetched_document_bytes_are_primary(self):
        """A real tool call returning real document bytes → PRIMARY. This is
        the construction site the old orchestrator never had; VERIFIED tier
        becomes reachable for genuinely fetched primary documents."""
        led = ProvenanceLedger()
        body = "<html>official box score ...</html>"
        led.record_tool_result("web_fetch", body, primary=True,
                               urls=["https://official.example/scores"])
        assert led.assign_source_class(_ev(body)) is SourceClass.PRIMARY

    def test_search_result_bytes_are_secondary(self):
        led = ProvenanceLedger()
        snippet = "Pinnacle closed -3.5 for game X"
        led.record_tool_result("web_search", snippet)
        assert led.assign_source_class(_ev(snippet)) is SourceClass.SECONDARY

    def test_model_assertion_is_inferred_even_when_declared_secondary(self):
        """The core fix: the label is a function of the code path. Text the
        model produced with no matching tool return is INFERRED, period."""
        led = ProvenanceLedger()
        led.record_tool_result("web_search", "unrelated bytes")
        assert led.assign_source_class(
            _ev("I recall that team X always covers", declared=SourceClass.SECONDARY)
        ) is SourceClass.INFERRED

    def test_evidence_citing_fetched_url_is_secondary(self):
        led = ProvenanceLedger()
        led.record_tool_result("web_fetch", "doc body", urls=["https://s.example/r"])
        assert led.assign_source_class(
            _ev("https://s.example/r reports -3.5")
        ) is SourceClass.SECONDARY


class TestClampByProvenance:
    def test_declared_primary_provenance_inferred_is_clamped_to_055(self):
        got = clamp_confidence_provenance(0.95, SourceClass.INFERRED, MAX_BY)
        assert got == 0.55

    def test_primary_bytes_can_reach_verified(self):
        got = clamp_confidence_provenance(0.95, SourceClass.PRIMARY, MAX_BY)
        assert got == 0.95  # >= 0.90 → VERIFIED tier, now reachable


class TestRelabelEvidence:
    def test_demotion_counted_and_confidence_clamped(self):
        led = ProvenanceLedger()
        evs = [
            _ev("fabricated claim with https://fake.example/x", conf=0.75),
            _ev("real snippet", conf=0.70),
        ]
        led.record_tool_result("web_search", "real snippet")
        demoted = relabel_evidence(evs, led, MAX_BY)
        assert demoted == 1
        assert evs[0].source_class is SourceClass.INFERRED
        assert evs[0].confidence_score == 0.55
        assert evs[1].source_class is SourceClass.SECONDARY

    def test_promotion_of_real_primary_bytes(self):
        led = ProvenanceLedger()
        body = "primary doc"
        led.record_tool_result("web_fetch", body, primary=True)
        ev = _ev(body, declared=SourceClass.INFERRED, conf=0.50)
        assert relabel_evidence([ev], led, MAX_BY) == 0
        assert ev.source_class is SourceClass.PRIMARY
        # floored at DB floor, not raised to ceiling — provenance sets the
        # ceiling, the model's own honesty sets the value under it.
        assert ev.confidence_score == 0.50
