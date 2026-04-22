"""Tests for AGP protocol core — enums, sessions, evidence, sealing."""

import pytest
from agp import (
    AGPSession,
    AGPViolation,
    ConfidenceTier,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)


class TestConfidenceTier:
    def test_from_score_verified(self):
        assert ConfidenceTier.from_score(0.95) == ConfidenceTier.VERIFIED
        assert ConfidenceTier.from_score(0.90) == ConfidenceTier.VERIFIED

    def test_from_score_corroborated(self):
        assert ConfidenceTier.from_score(0.80) == ConfidenceTier.CORROBORATED
        assert ConfidenceTier.from_score(0.75) == ConfidenceTier.CORROBORATED

    def test_from_score_probable(self):
        assert ConfidenceTier.from_score(0.60) == ConfidenceTier.PROBABLE
        assert ConfidenceTier.from_score(0.55) == ConfidenceTier.PROBABLE

    def test_from_score_speculative(self):
        assert ConfidenceTier.from_score(0.40) == ConfidenceTier.SPECULATIVE
        assert ConfidenceTier.from_score(0.30) == ConfidenceTier.SPECULATIVE

    def test_from_score_unverified(self):
        assert ConfidenceTier.from_score(0.29) == ConfidenceTier.UNVERIFIED
        assert ConfidenceTier.from_score(0.0) == ConfidenceTier.UNVERIFIED

    def test_storable(self):
        assert ConfidenceTier.VERIFIED.is_storable is True
        assert ConfidenceTier.CORROBORATED.is_storable is True
        assert ConfidenceTier.PROBABLE.is_storable is True
        assert ConfidenceTier.SPECULATIVE.is_storable is True
        assert ConfidenceTier.UNVERIFIED.is_storable is False


class TestEvidence:
    def test_signal_cannot_promote(self):
        ev = Evidence(
            content="test", source_class=SourceClass.SIGNAL,
            confidence_score=0.9, domain=Domain.TECHNICAL, origin_agent="test",
        )
        assert ev.can_promote_to_conclusion() is False

    def test_primary_can_promote(self):
        ev = Evidence(
            content="test", source_class=SourceClass.PRIMARY,
            confidence_score=0.9, domain=Domain.TECHNICAL, origin_agent="test",
        )
        assert ev.can_promote_to_conclusion() is True

    def test_unverified_cannot_promote(self):
        ev = Evidence(
            content="test", source_class=SourceClass.PRIMARY,
            confidence_score=0.1, domain=Domain.TECHNICAL, origin_agent="test",
        )
        assert ev.can_promote_to_conclusion() is False

    def test_to_dict_roundtrip(self):
        ev = Evidence(
            content="finding", source_class=SourceClass.SECONDARY,
            confidence_score=0.75, domain=Domain.FINANCIAL,
            origin_agent="architect", source_name="reuters",
        )
        d = ev.to_dict()
        assert d["source_class"] == "SECONDARY"
        assert d["confidence_tier"] == "CORROBORATED"
        assert d["domain"] == "FINANCIAL"


class TestAGPSession:
    def test_sequential_advancement(self):
        s = AGPSession("test query")
        assert s.current_step == SessionStep.DECLARE_SCOPE
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        assert s.current_step == SessionStep.ASSIGN_DOMAIN

    def test_cannot_skip_steps(self):
        s = AGPSession("test query")
        with pytest.raises(AGPViolation):
            s.advance_to(SessionStep.SOURCE_ENUMERATION)  # skips ASSIGN_DOMAIN

    def test_unverified_evidence_filtered(self):
        s = AGPSession("test")
        ev = Evidence(
            content="weak", source_class=SourceClass.INFERRED,
            confidence_score=0.1, domain=Domain.GENERAL, origin_agent="test",
        )
        s.add_evidence(ev)
        assert len(s.evidence) == 0  # silently filtered

    def test_storable_evidence_kept(self):
        s = AGPSession("test")
        ev = Evidence(
            content="solid", source_class=SourceClass.PRIMARY,
            confidence_score=0.8, domain=Domain.TECHNICAL, origin_agent="test",
        )
        s.add_evidence(ev)
        assert len(s.evidence) == 1

    def test_seal_requires_session_close(self):
        s = AGPSession("test")
        with pytest.raises(AGPViolation, match="SESSION_CLOSE"):
            s.seal()

    def test_seal_requires_summary(self):
        s = AGPSession("test")
        s.domain = Domain.GENERAL
        for step in list(SessionStep)[1:]:
            s.advance_to(step)
        with pytest.raises(AGPViolation, match="summary"):
            s.seal()

    def _good_evidence(self, domain=Domain.GENERAL):
        return Evidence(
            content="supporting fact", source_class=SourceClass.SECONDARY,
            confidence_score=0.7, domain=domain, origin_agent="test",
        )

    def test_full_session_seals(self):
        s = AGPSession("test")
        s.domain = Domain.TECHNICAL
        # Walk through all steps; add real evidence so seal() accepts it.
        # Post-rigor upgrade, seal() refuses empty-evidence/empty-conclusion
        # sessions — see tests/test_agp_seal.py for the refuse-to-seal tests.
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        s.add_evidence(self._good_evidence(Domain.TECHNICAL))
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="test", domain=Domain.TECHNICAL,
            conclusion="result", confidence_score=0.6,
            evidence_count=1, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        seal = s.seal()
        assert len(seal) == 64  # SHA-256 hex
        assert s.sealed_at is not None

    def test_cannot_modify_sealed_session(self):
        s = AGPSession("test")
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        s.add_evidence(self._good_evidence())
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="test", domain=Domain.GENERAL,
            conclusion="done", confidence_score=0.5,
            evidence_count=1, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        s.seal()
        with pytest.raises(AGPViolation, match="sealed"):
            s.add_evidence(Evidence(
                content="late", source_class=SourceClass.PRIMARY,
                confidence_score=0.9, domain=Domain.GENERAL, origin_agent="test",
            ))

    def test_double_seal_rejected(self):
        s = AGPSession("test")
        s.domain = Domain.GENERAL
        s.advance_to(SessionStep.ASSIGN_DOMAIN)
        s.advance_to(SessionStep.SOURCE_ENUMERATION)
        s.advance_to(SessionStep.PRIMARY_COLLECTION)
        s.add_evidence(self._good_evidence())
        s.advance_to(SessionStep.CONTRADICTION_CHECK)
        s.advance_to(SessionStep.SYNTHESIS)
        s.summary = SessionSummary(
            scope="test", domain=Domain.GENERAL,
            conclusion="done", confidence_score=0.5,
            evidence_count=1, contradiction_count=0,
        )
        s.advance_to(SessionStep.SESSION_CLOSE)
        s.seal()
        with pytest.raises(AGPViolation, match="already sealed"):
            s.seal()
