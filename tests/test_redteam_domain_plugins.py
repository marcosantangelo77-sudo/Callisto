"""RED TEAM — domain plugins / routing seams / 200-with-error-body.

Rotation pass 2026-08-25. Surface: domain plugins and routing (explicitly
unattacked ground), attacked where they meet the provenance ledger and
relevance gate. Method: adversarial input at the seams + wiring audit
(family hunt per PATTERNS.md #1 "what calls this?" and #3 "feed every gate
an EMPTY input").

Confirmed defects, each with a failing-on-current-code test:

  R1 CRITICAL — a source that returns HTTP 200 with an ERROR BODY is
     minted PRIMARY by RestSource._record, admitted by RelevanceGate on
     the error text's own topical words, and the pipeline SEALS at
     PROBABLE/AFFIRMS on it. Families 3 + 9 (and the live-run "200 with
     zero results" defect recurring one layer deeper).
  R2 HIGH    — same root, unit level: any non-200 body whose transport
     does not raise (proxies that return 200-with-error-page, injected
     test transports, HTML interstitials) becomes PRIMARY evidence bytes.
     Nothing between RestSource and the gate checks status or shape.
  R3 HIGH    — three DomainPlugins are built, tested, and NEVER registered
     in production: finance, kalshi, source_registry. The extension point
     exists; nothing calls register_if_available for them (family 1:
     inert mechanism — "registration IS the extension point" that nobody
     extends through).
  R4 MEDIUM  — the source registry registers kalshi/cmefedfut/sec_fulltext
     adapters, but tools.sources.query_builder has NO planner for them
     ("unknown source"). Selection can pick a source retrieval can never
     route; budget and independence accounting see an honest gap that
     selection never told the user about.
  R5 MEDIUM  — KalshiMarket.resolved_outcome() returns 'yes'/'no' from the
     result STRING even while status='active' (family 4: a label standing
     in for settlement). Only is_settled() requires both; every consumer
     of resolved_outome() — including the adapter's resolution() marketed
     as ground truth — reads an unsettled contract as resolved.

All fixtures, no sockets. No confidence was raised anywhere except in
demonstrating that the system raises its own.
"""
from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path

import pytest

from tests.helpers.no_socket import NoSocket

_nosocket = NoSocket()
_nosocket.install()

from agp import Domain  # noqa: E402
from agp.provenance import ProvenanceLedger  # noqa: E402
from tools.artifacts import ArtifactStore  # noqa: E402
from tools.pipeline.engine import ResearchPipeline, fixture_transport  # noqa: E402
from tools.pipeline.model import ScriptedModel  # noqa: E402
from tools.sources.base import SourceSpec, RestSource  # noqa: E402


# ── R1/R2 fixtures ──────────────────────────────────────────────────────────

#: An upstream failure returned INSIDE a 200 response. Its text contains
#: the question's topical words because real API error messages do
#: ("market data unavailable for fed decision markets").
ERROR_BODY = json.dumps({
    "error": "market probability fed decision: upstream failure",
    "results": [],
    "meta": {"error": True},
})

QUESTION = "what is the market probability of the fed decision?"

DECOMP_N1 = json.dumps({"sub_questions": [
    {"text": QUESTION,
     "kind": "descriptive", "question_type": "scholarly work search",
     "min_source_tier": 2, "min_independent_sources": 1,
     "quant_required": False}]})

ANSWER = json.dumps({
    "answer": "sources indicate the market prices about 40 percent",
    "proposed_confidence": 0.8, "stance": "AFFIRMS", "compute": None})


class _QuietAdversary:
    async def complete(self, task_class, messages, schema=None):
        return {"parsed_json": {"objections": []}, "model": "stub"}


def _pipeline(tmp_path, routes):
    model = ScriptedModel({
        "Architect": [{"content": DECOMP_N1}],
        "Manager": [{"content": ANSWER}, {"content": ANSWER}],
    })
    return ResearchPipeline(
        model=model, adversary_router=_QuietAdversary(),
        transport=fixture_transport(routes),
        store=ArtifactStore(root=tmp_path / "artifacts"))


# ── R2 (unit): non-200 bodies are minted PRIMARY and parsed as data ────────

class TestR2ErrorBodyMintedPrimary:
    def test_503_body_is_recorded_primary_and_parsed(self):
        led = ProvenanceLedger()
        spec = SourceSpec(name="kalshi", base_url="https://x.example",
                          description="", answers=("m",), cannot_answer=("c",),
                          tier=3, min_interval_s=0.0)
        src = RestSource(spec, ledger=led, transport=lambda u, h: (503, ERROR_BODY))
        data, rec = src.get_json(src.build_url("/markets"))
        # The transport DID report failure — but the caller still received a
        # parsed dict, and the ledger minted the error bytes PRIMARY.
        assert rec.status == 503
        assert led.is_primary_bytes(ERROR_BODY) is False, (
            "R2: an HTTP-503 body must not become PRIMARY provenance bytes; "
            "get_json must raise or the recorder must skip non-200 bodies")

    def test_200_error_envelope_is_distinguishable_from_data(self):
        """An envelope with error+empty results must not be treated as a
        successful empty page. Today nothing anywhere inspects the shape."""
        led = ProvenanceLedger()
        spec = SourceSpec(name="openalex", base_url="https://x.example",
                          description="", answers=("m",), cannot_answer=("c",),
                          tier=2, min_interval_s=0.0)
        src = RestSource(spec, ledger=led,
                         transport=lambda u, h: (200, ERROR_BODY))
        data, rec = src.get_json(src.build_url("/works"))
        assert data.get("results") == []
        assert not data.get("error"), (
            "R1/R2: a 200 response carrying an 'error' key must surface as "
            "an error to callers, not parse as a legitimate empty result")


# ── R1 (end-to-end): the run seals PROBABLE on an upstream error ───────────

class TestR1PipelineSealsOnErrorBody:
    def test_error_body_cannot_reach_seal_as_primary_evidence(self, tmp_path):
        pipe = _pipeline(tmp_path, {"/works": ERROR_BODY})
        res = asyncio.run(pipe.run(QUESTION, domain=Domain.GENERAL))
        assert not res.sealed, (
            "R1: a run whose ONLY fetch was an HTTP-200 error envelope "
            "(zero results) must not seal. It sealed at "
            f"{res.confidence_tier} {res.confidence_score} stance={res.stance} "
            "on the error text itself")
        if res.leaves:
            leaf = res.leaves[0]
            assert "PRIMARY" not in leaf.source_classes, (
                "R1: the error envelope was provenance-minted PRIMARY and "
                "admitted as the leaf's evidence")

    def test_gate_judges_content_not_error_markers(self, tmp_path):
        """Direct demonstration of the amplifier: the relevance gate cannot
        tell an error message from a document, because it scores token
        coverage over ALL strings in the payload including the 'error' one."""
        from tools.pipeline.retrieval import RelevanceGate
        gate = RelevanceGate(min_coverage=0.25)
        ok, cov, reason = gate.judge(QUESTION, "", json.loads(ERROR_BODY))
        assert not ok or "error" in reason.lower(), (
            f"R1-amplifier: gate admitted an error envelope (coverage "
            f"{cov:.2f}) scoring its error message's own words")


# ── R3 (wiring): built plugins that production never registers ─────────────

class TestR3UnregisteredPlugins:
    def test_production_registry_serves_every_built_plugin(self):
        """finance, kalshi and source-registry plugins exist, carry tested
        tool schemas, and are registered NOWHERE in production code — only
        their own tests call register_if_available. The orchestrator seeds
        sports + compute and stops."""
        from orchestrator import _default_registry
        names = {p.name for p in _default_registry().plugins()}
        missing = {"finance", "kalshi", "sources"} - names
        assert not missing, (
            f"R3 (family 1): DomainPlugins built and tested but never "
            f"registered in production: {sorted(missing)}. A FINANCIAL "
            f"session receives no edgar tools; no session receives "
            f"kalshi_market_edge or source_registry_list")


# ── R4: registry selects sources the router cannot serve ───────────────────

class TestR4SelectedButUnroutable:
    def test_every_registered_adapter_has_a_query_plan_or_honest_gap(self):
        from tools.sources.registry import get_source_registry
        from tools.sources import query_builder
        reg = get_source_registry()
        unknown = [s["name"] if isinstance(s, dict) else s.name
                   for s in reg.specs()
                   if query_builder.build_plan(
                       s["name"] if isinstance(s, dict) else s.name,
                       "any question") .plannable is False
                   and "unknown source" in (query_builder.build_plan(
                       s["name"] if isinstance(s, dict) else s.name,
                       "any question").reason or "")]
        assert not unknown, (
            f"R4: registered adapters with NO query plan ('unknown source'): "
            f"{unknown}. Selection can route to a source retrieval must skip; "
            f"declare an honest gap instead")


# ── R5: an unsettled contract reports a resolution ─────────────────────────

class TestR5PrematureResolution:
    def test_resolved_outcome_requires_settlement(self):
        from tools.domains.kalshi.market import KalshiMarket
        m = KalshiMarket.from_api({"ticker": "KX-2", "result": "YES",
                                   "status": "active"})
        assert m.resolved_outcome() is None, (
            "R5 (family 4): resolved_outcome() returns the result STRING on "
            "an ACTIVE contract. Only is_settled() checks status; every "
            "consumer of resolved_outcome() — including KalshiAdapter."
            "resolution(), sold as settlement ground truth — reads an "
            "unsettled market as resolved")
