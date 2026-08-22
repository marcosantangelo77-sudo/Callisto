"""Tier 3 epistemics — independent seal-veto hook (Sentinel-shaped).

Characterization first: today AGPSession.seal() consults only its own
garbage gates. There is no way for a component that did not write the
conclusion to refuse a seal, so "the Sentinel" governs nothing. These tests
pin the NEW behavior: seal() accepts an optional independent reviewer; if
the reviewer returns a non-empty reason string, the seal is REFUSED with
AGPSealRefused and nothing is hashed.
"""
import pytest

from agp import (
    AGPSession,
    AGPSealRefused,
    Domain,
    Evidence,
    SessionStep,
    SessionSummary,
    SourceClass,
)


def _session_at_close(conclusion="a real conclusion") -> AGPSession:
    s = AGPSession("veto probe")
    s.advance_to(SessionStep.ASSIGN_DOMAIN); s.domain = Domain.GENERAL
    s.advance_to(SessionStep.SOURCE_ENUMERATION); s.sources = ["x"]
    s.advance_to(SessionStep.PRIMARY_COLLECTION)
    s.add_evidence(Evidence(
        content="observed fact", source_class=SourceClass.SECONDARY,
        confidence_score=0.70, domain=Domain.GENERAL, origin_agent="t"))
    s.advance_to(SessionStep.CONTRADICTION_CHECK)
    s.advance_to(SessionStep.SYNTHESIS)
    s.summary = SessionSummary(
        scope="q", domain=Domain.GENERAL, conclusion=conclusion,
        confidence_score=0.70, evidence_count=1, contradiction_count=0)
    s.advance_to(SessionStep.SESSION_CLOSE)
    return s


class TestNoVetoBackcompat:
    def test_seal_without_reviewer_unchanged(self):
        assert _session_at_close().seal()

    def test_seal_with_approving_reviewer_succeeds(self):
        s = _session_at_close()
        s.seal_veto = lambda session, summary: None
        assert s.seal()


class TestVetoPower:
    def test_vetoed_session_refuses_and_does_not_hash(self):
        s = _session_at_close()
        def reviewer(session, summary):
            return ("conclusion asserts X but no evidence item mentions X")
        s.seal_veto = reviewer
        with pytest.raises(AGPSealRefused, match="independent review"):
            s.seal()
        assert s.seal_hash is None
        assert s._sealed is False

    def test_veto_reason_is_surfaced(self):
        s = _session_at_close()
        s.seal_veto = lambda session, summary: "evidence contains no mention of claim"
        with pytest.raises(AGPSealRefused, match="no mention of claim"):
            s.seal()

    def test_empty_string_veto_means_approval(self):
        s = _session_at_close()
        s.seal_veto = lambda session, summary: ""
        assert s.seal()

    def test_broken_reviewer_fails_closed(self):
        """A reviewer that raises must not silently become an approval —
        fail closed, refuse the seal."""
        s = _session_at_close()
        def broken(session, summary):
            raise RuntimeError("reviewer crashed")
        s.seal_veto = broken
        with pytest.raises(AGPSealRefused):
            s.seal()
