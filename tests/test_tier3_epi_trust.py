"""Tier 3 epistemics — trust-escalator and tier-reachability characterization.

These tests PIN the current (defective) behavior of the wiki/hermes trust
loop and the source-class ceilings, so any future repair shows up as a
deliberate diff rather than silent drift. Each test documents the defect it
characterizes. Instance 4, audit 2026-08-22.
"""
import json
import math

import pytest

from agp import (
    AGPSession,
    ConfidenceTier,
    Domain,
    Evidence,
    SessionStep,
    SourceClass,
)
from agp.thresholds import (
    MAX_CONFIDENCE_BY_SOURCE,
    TIER_CORROBORATED_MIN,
    TIER_PROBABLE_MIN,
)


class TestCeilingsPinned:
    """Pin the ceiling table itself — the load-bearing constant."""

    def test_ceiling_table_matches_documented_values(self):
        assert MAX_CONFIDENCE_BY_SOURCE == {
            "PRIMARY": 1.0,
            "SECONDARY": 0.75,
            "SIGNAL": 0.55,
            "INFERRED": 0.55,
        }

    def test_tier_boundaries(self):
        assert ConfidenceTier.from_score(0.90).name == "VERIFIED"
        assert ConfidenceTier.from_score(0.75).name == "CORROBORATED"
        assert ConfidenceTier.from_score(0.7499).name == "PROBABLE"
        assert ConfidenceTier.from_score(0.55).name == "PROBABLE"


class TestVERIFIEDUnreachable:
    """ROADMAP C2: VERIFIED requires score >= 0.90, only PRIMARY ceiling is 1.0,
    but no AGP-session path ever assigns SourceClass.PRIMARY to evidence.
    Orchestrator assigns SECONDARY (web) or INFERRED (reasoning) — see
    orchestrator.py:1768/1799; the collection prompts at :1106/:1171 offer the
    model only SECONDARY/INFERRED. So VERIFIED is unreachable by construction.
    These tests pin the mechanism, not just the claim."""

    def test_orchestrator_prompt_offers_only_secondary_inferred(self):
        import inspect
        import orchestrator
        src = inspect.getsource(orchestrator)
        # The evidence-collection JSON schemas never offer PRIMARY as a choice.
        assert '"source_class":"SECONDARY"' in src or "'source_class': 'SECONDARY'" in src
        # No construction site passes source_class=PRIMARY into Evidence.
        assert "Evidence(\n" in src  # sanity: Evidence sites exist
        assert "source_class=SourceClass.PRIMARY" not in src

    def test_session_with_max_secondary_evidence_cannot_reach_verified(self):
        s = _sealed_session(best_conf=0.75)
        assert s.summary.confidence_tier is not VERIFIED_IF_AVAILABLE


VERIFIED_IF_AVAILABLE = ConfidenceTier.VERIFIED


def _make_raw(query="escalator probe") -> AGPSession:
    s = AGPSession(query)
    s.advance_to(SessionStep.ASSIGN_DOMAIN); s.domain = Domain.GENERAL
    s.advance_to(SessionStep.SOURCE_ENUMERATION); s.sources = ["x"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    return s


def _sealed_session(evidence_confs=(0.70,), best_conf=0.70):
    from agp import SessionSummary
    s = _make_raw()
    for c in evidence_confs:
        s.add_evidence(Evidence(
            content="fact", source_class=SourceClass.SECONDARY,
            confidence_score=c, domain=Domain.GENERAL, origin_agent="t",
        ))
    s.summary = SessionSummary(
        scope="q", domain=Domain.GENERAL, conclusion="c",
        confidence_score=best_conf, evidence_count=len(evidence_confs),
        contradiction_count=0,
    )
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


class TestTrustEscalatorArithmetic:
    """The wiki/hermes loop: hermes learnings (self-reported confidence >= 0.5,
   ratcheted upward by MAX(confidence, excluded.confidence) on every rewrite —
    knowledge_wiki.py:244 filters them at confidence >= 0.5 and averages them
    into article confidence (knowledge_wiki.py:375), and autonomous.py injects
    articles back as PRIOR KNOWLEDGE prompt context. Nothing checks a seal or
    an external source anywhere on that cycle. These tests pin the numbers."""

    def test_hermes_ratchet_is_monotonic_upward(self):
        """ON CONFLICT ... confidence=MAX(confidence, excluded.confidence):
        re-reporting the same learning can never lower its confidence, so one
        optimistic write permanently contaminates the key."""
        # Pin the SQL text so a repair (e.g. decay, or min()) shows as a diff.
        import inspect
        from tools.hermes_memory import HermesMemory
        src = inspect.getsource(HermesMemory.record_learning)
        assert "confidence=MAX(confidence, excluded.confidence)" in src

    def test_wiki_compile_ingests_unverified_learnings_at_half_ceiling(self):
        """REPAIRED 2026-08-22 (instance4 implementation pass): wiki compile
        now seal-gates session ingestion — a row whose seal_hash fails
        verify_seal is REJECTED; legacy unsealed rows enter capped at the
        INFERRED ceiling. See tests/test_tier3_epi_wiki_ingestion.py. This
        pin is updated to assert the repair, not the defect."""
        import inspect
        from tools import knowledge_wiki as kw
        from tools.wiki.compiler import WikiCompiler
        # 2026-08 split: source ingestion moved to tools.wiki.compiler.
        src = inspect.getsource(WikiCompiler._get_uncompiled_sources)
        assert "verify_seal" in src            # seal gate exists at ingestion
        assert "INFERRED" in src               # legacy rows capped as INFERRED
        # Article confidence is min-of-sources, not mean (no manufactured
        # corroboration).
        src_create = inspect.getsource(WikiCompiler._create_article)
        assert "_article_confidence(sources)" in src_create

    def test_article_confidence_min_of_sources_not_mean(self):
        """REPAIRED: _create_article uses min-of-sources. Two identical 0.75
        SECONDARY items no longer manufacture CORROBORATION beyond what one
        item carries — and a weak source caps the whole article."""
        from tools.knowledge_wiki import _article_confidence
        assert _article_confidence([{"confidence": 0.75}, {"confidence": 0.75}]) == 0.75
        mixed = [0.75, 0.75]
        avg = sum(mixed) / len(mixed)          # old behavior for comparison
        assert ConfidenceTier.from_score(avg) is ConfidenceTier.CORROBORATED
        # with min rule + differing sources, article cannot exceed weakest:
        assert _article_confidence([{"confidence": 0.75}, {"confidence": 0.50}]) == 0.50

    def test_weighted_merge_decay_is_glacial_but_not_zero(self):
        """FALSIFIED HYPOTHESIS, recorded honestly: we expected the
        old*weight+new/(weight+1) merge to make article tiers unable to fall.
        Arithmetic says otherwise — it falls, just slowly. After FIVE
        consecutive all-garbage (conf 0.30) compiles a 0.78 article is still
        in the PROBABLE band; the escalator lives at INGESTION (unverified
        learnings entering at conf>=0.5), not at this merge."""
        existing_conf = 0.78
        weight = 10
        for new_src_conf in (0.30,) * 5:
            existing_conf = (existing_conf * weight + new_src_conf) / (weight + 1)
        assert existing_conf > TIER_PROBABLE_MIN  # 0.686 after 5 garbage rounds
        # and 20 straight garbage rounds WOULD pull it to ~0.37:
        c, w = 0.78, 10
        for _ in range(20):
            c = (c * w + 0.30) / (w + 1)
        assert c < TIER_PROBABLE_MIN


class TestCitationCheckVacuity:
    """REPAIRED (build/tool-registry): `_response_cites_urls` is deleted.
    Citation grounding now routes through the per-session ProvenanceLedger
    (`agp/provenance.py`): a citation counts only if it names a URL the
    session actually fetched. These tests pin the replacement property:
    a fabricated URL — or the bare string 'http://' — buys nothing."""

    def test_ledger_rejects_fabricated_url(self):
        from agp.provenance import ProvenanceLedger
        ledger = ProvenanceLedger()
        ledger.record_tool_result(
            "web_search", "real result", urls=["https://actually-fetched.example.com/a"]
        )
        assert not ledger.cites_verified_url(
            "see https://totally-fabricated.example.net/x"
        )
        assert not ledger.cites_verified_url("the string http:// appears here")
        assert ledger.cites_verified_url("per https://actually-fetched.example.com/a")

    def test_upgrade_mechanism(self):
        from orchestrator import MAX_CONFIDENCE_BY_SOURCE
        gain = (MAX_CONFIDENCE_BY_SOURCE["SECONDARY"]
                - MAX_CONFIDENCE_BY_SOURCE["INFERRED"])
        assert math.isclose(gain, 0.20)
